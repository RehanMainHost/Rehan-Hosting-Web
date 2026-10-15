"""Bot runtime + auto-restart + env/config detect + log rings. Hardened."""
import asyncio, zipfile, io, re, json, os, traceback, time
from collections import deque
from datetime import datetime

# ============================================================
# app log ring
# ============================================================
_app_logs: dict = {}

def applog(app_id: str, level: str, msg: str):
    ring = _app_logs.setdefault(app_id, deque(maxlen=500))
    ring.append({"ts": datetime.utcnow().strftime("%H:%M:%S"),
                 "level": level, "msg": str(msg)})

def applog_snapshot(app_id: str):
    return list(_app_logs.get(app_id, []))

# ============================================================
# parsers
# ============================================================
def _parse_env_text(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        if "=" not in line: continue
        k, v = line.split("=", 1)
        k = k.strip(); v = v.strip().strip('"').strip("'")
        if k: out[k] = v
    return out

def _parse_python_config(text: str) -> dict:
    out = {}
    for m in re.finditer(r'^([A-Z][A-Z0-9_]{1,})\s*=\s*(.+?)\s*(?:#.*)?$', text or "", re.M):
        k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
        out[k] = v
    return out

def _parse_json_config(text: str) -> dict:
    try:
        data = json.loads(text or "")
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items()
                    if isinstance(v, (str, int, float, bool))}
    except Exception:
        pass
    return {}

# ============================================================
# bundle extractor
# ============================================================
def extract_bundle(zip_bytes: bytes):
    files: dict = {}
    env: dict = {}
    entry_src = ""
    entry_name = ""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if name.endswith("/"): continue
            base = os.path.basename(name)
            if "__MACOSX" in name or base.startswith("."): continue
            try:
                raw = z.read(name)
            except Exception:
                continue
            files[name] = raw
            low = base.lower()
            try:
                if low in (".env", ".env.example", ".env.local", "env.txt"):
                    env.update(_parse_env_text(raw.decode("utf-8", "ignore")))
                elif low in ("config.py", "settings.py", "conf.py"):
                    env.update(_parse_python_config(raw.decode("utf-8", "ignore")))
                elif low in ("config.json", "settings.json"):
                    env.update(_parse_json_config(raw.decode("utf-8", "ignore")))
            except Exception:
                pass
    for cand in ("bot.py", "main.py", "app.py", "__main__.py"):
        for n in files:
            if os.path.basename(n) == cand and n.endswith(".py"):
                entry_src = files[n].decode("utf-8", "ignore")
                entry_name = n
                break
        if entry_src: break
    if not entry_src:
        for n in files:
            if n.endswith(".py"):
                entry_src = files[n].decode("utf-8", "ignore")
                entry_name = n
                break
    return entry_src, entry_name, env, files

# ============================================================
# token + name
# ============================================================
_TOKEN_PATTERNS = [re.compile(r'\b(\d{8,12}:[A-Za-z0-9_-]{30,})\b')]
_TOKEN_KEYS = ("BOT_TOKEN", "TELEGRAM_TOKEN", "TG_TOKEN", "TOKEN",
               "BOT_API_TOKEN", "API_TOKEN", "TELEGRAM_BOT_TOKEN")

def detect_token(env: dict, source: str = "") -> str:
    for k in _TOKEN_KEYS:
        v = env.get(k)
        if v and ":" in v:
            return v.strip().strip('"').strip("'")
    for k, v in env.items():
        if "token" in k.lower() and ":" in (v or ""):
            return v.strip().strip('"').strip("'")
    for pat in _TOKEN_PATTERNS:
        m = pat.search(source or "")
        if m:
            return m.group(1)
    return ""

def name_from_filename(fn: str) -> str:
    base = os.path.basename(fn or "")
    for ext in (".zip", ".py", ".tar", ".gz", ".tgz", ".rar"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
    return base or "bot"

# ============================================================
# app entry detection
# ============================================================
_ENTRY_CANDIDATES = [
    ("manage.py", "django",      "python manage.py runserver 0.0.0.0:8000"),
    ("app.py",    "flask",       "python app.py"),
    ("main.py",   "python",      "python main.py"),
    ("server.py", "python",      "python server.py"),
    ("wsgi.py",   "python-wsgi", "gunicorn wsgi:application"),
    ("index.js",  "node",        "node index.js"),
    ("server.js", "node",        "node server.js"),
    ("app.js",    "node",        "node app.js"),
    ("main.go",   "go",          "go run main.go"),
]

def detect_entry(files: dict) -> dict:
    result = {"entry": "", "kind": "unknown", "run_cmd": "", "port": 8000, "static_root": ""}
    for path, raw in files.items():
        if path.endswith("Procfile"):
            try:
                for line in raw.decode("utf-8", "ignore").splitlines():
                    if line.startswith("web:"):
                        result["run_cmd"] = line.split(":", 1)[1].strip()
                        result["kind"] = "procfile"
                        result["entry"] = "Procfile"
                        return result
            except Exception:
                pass
    for path, raw in files.items():
        if path.split("/")[-1] == "package.json":
            try:
                pkg = json.loads(raw.decode("utf-8", "ignore"))
                if "start" in (pkg.get("scripts") or {}):
                    result["run_cmd"] = "npm start"
                    result["kind"] = "node"
                    result["entry"] = path
                    return result
            except Exception:
                pass
    def depth(p): return p.count("/")
    for fname, kind, cmd in _ENTRY_CANDIDATES:
        matches = sorted([p for p in files if p.endswith("/" + fname) or p == fname], key=depth)
        if matches:
            result["entry"] = matches[0]
            result["kind"] = kind
            result["run_cmd"] = cmd
            return result
    php_files = sorted([p for p in files if p.lower().endswith(".php")], key=depth)
    if php_files:
        idx = next((p for p in php_files if p.split("/")[-1].lower() == "index.php"), php_files[0])
        result["entry"] = idx
        result["kind"] = "php"
        result["run_cmd"] = "php-cgi (built-in runtime)"
        result["static_root"] = idx.rsplit("/", 1)[0] if "/" in idx else ""
        return result
    for path in files:
        if path.endswith("index.html"):
            result["entry"] = path
            result["kind"] = "static"
            result["static_root"] = path.rsplit("index.html", 1)[0].rstrip("/")
            return result
    return result

# ============================================================
# BotRuntime — auto-restart hardened
# ============================================================
CRASH_WINDOW   = 60.0   # seconds
CRASH_THRESHOLD = 5     # crashes in window → restart bot
RESTART_WINDOW = 300.0  # seconds
RESTART_MAX    = 5      # max restarts in window before dormant
WATCHDOG_TICK  = 10.0   # seconds

class BotRuntime:
    def __init__(self, bot_id, name, token, code, filename, env=None, mode="polling"):
        self.bot_id = bot_id
        self.name = name
        self.token = token or ""
        self.code = code or ""
        self.filename = filename or "bot.py"
        self.env = env or {}
        self.mode = mode
        self.task = None
        self.watchdog = None
        self.logs = deque(maxlen=800)
        self.running = False
        self.dormant = False
        self.handler = None
        self._offset = 0
        self.webhook_secret = ""
        self.started_at = 0.0
        self.cycles = 0
        self.errors = 0
        self.restarts = 0
        self.auto_restart = True
        self._crash_count = 0
        self._crash_window_start = 0.0
        self._restart_times: list = []
        self._lock = asyncio.Lock()

    def log(self, level, msg):
        self.logs.append({"ts": datetime.utcnow().strftime("%H:%M:%S"),
                          "level": level, "msg": str(msg)})

    def load(self):
        mod = type("BotModule", (), {})()
        ns = {
            "BOT_TOKEN": self.token,
            "BOT_ID": self.bot_id,
            "log": lambda m: self.log("info", m),
            "__name__": f"bot_{self.bot_id}",
        }
        for k, v in self.env.items():
            ns.setdefault(k, v)
        mod.__dict__.update(ns)
        try:
            exec(compile(self.code, self.filename, "exec"), mod.__dict__)
        except Exception:
            self.log("error", "load failed:\n" + traceback.format_exc())
            raise
        self.handler = getattr(mod, "handle_update", None)
        if self.handler is None:
            raise RuntimeError("bot code must define: async def handle_update(update): ...")

    # ---------- crash tracking ----------
    def _record_crash(self):
        now = time.time()
        if now - self._crash_window_start > CRASH_WINDOW:
            self._crash_window_start = now
            self._crash_count = 0
        self._crash_count += 1
        return self._crash_count

    def _reset_crash(self):
        now = time.time()
        if now - self._crash_window_start > CRASH_WINDOW:
            self._crash_count = 0
            self._crash_window_start = now

    def _can_restart(self) -> bool:
        now = time.time()
        self._restart_times = [t for t in self._restart_times if now - t < RESTART_WINDOW]
        return len(self._restart_times) < RESTART_MAX

    async def _safe_handle(self, update):
        try:
            await self.handler(update)
            self._reset_crash()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.errors += 1
            self.log("error", "handler crash:\n" + traceback.format_exc())
            n = self._record_crash()
            if self.auto_restart and n >= CRASH_THRESHOLD:
                self.log("warn", f"{n} crashes in {int(CRASH_WINDOW)}s — auto-restart triggered")
                asyncio.create_task(self._auto_restart("handler crash storm"))

    # ---------- restart ----------
    async def _auto_restart(self, reason: str = ""):
        if not self.running: return
        async with self._lock:
            if not self.running: return
            if not self._can_restart():
                self.log("error",
                         f"restart limit reached ({RESTART_MAX} in {int(RESTART_WINDOW)}s) — "
                         f"bot DORMANT. fix code, then Start again.")
                self.dormant = True
                self.running = False
                return
            self._restart_times.append(time.time())
            self.restarts += 1
            self.log("warn", f"restarting (#{self.restarts}) — {reason or 'watchdog'}")
            # cancel current polling task
            if self.task and not self.task.done():
                self.task.cancel()
                try: await asyncio.wait_for(self.task, timeout=3)
                except (asyncio.CancelledError, asyncio.TimeoutError): pass
                except Exception: pass
            self.task = None
            self._crash_count = 0
            self._crash_window_start = time.time()
            # reload handler (fresh module namespace)
            try:
                self.handler = None
                self.load()
            except Exception as e:
                self.log("error", f"reload failed: {e} — staying down")
                self.running = False
                return
            # respawn
            try:
                if self.mode == "polling":
                    self.task = asyncio.create_task(self._run_polling())
                else:
                    await self._set_webhook()
                self.log("info", f"restart complete (#{self.restarts}) — running again")
            except Exception as e:
                self.log("error", f"respawn failed: {e}")
                self.running = False

    # ---------- polling ----------
    async def _run_polling(self):
        import httpx
        self.log("info", f"polling started — {self.name}  ({len(self.env)} env vars)")
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                while self.running:
                    try:
                        r = await client.get(
                            f"https://api.telegram.org/bot{self.token}/getUpdates",
                            params={"offset": self._offset + 1, "timeout": 25},
                        )
                        data = r.json()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.errors += 1
                        backoff = min(30, 2 ** min(self.errors, 5))
                        self.log("error", f"poll cycle failed: {e}  (retry in {backoff}s)")
                        await asyncio.sleep(backoff)
                        continue
                    if not data.get("ok"):
                        self.log("warn", f"telegram: {data.get('description', 'bad response')}")
                        await asyncio.sleep(4)
                        continue
                    self.errors = 0
                    self.cycles += 1
                    for u in data.get("result", []):
                        self._offset = u["update_id"]
                        await self._safe_handle(u)
        except asyncio.CancelledError:
            self.log("info", "polling cancelled")
            raise
        except Exception:
            self.log("error", "polling loop died:\n" + traceback.format_exc())
            if self.running and self.auto_restart:
                asyncio.create_task(self._auto_restart("polling loop died"))
            raise

    async def _set_webhook(self):
        import httpx
        from config import settings
        if not settings.PUBLIC_URL:
            self.log("warn", "PUBLIC_URL not set — falling back to polling")
            self.mode = "polling"
            self.task = asyncio.create_task(self._run_polling())
            return
        hook = f"{settings.PUBLIC_URL}/webhook/{self.bot_id}/{self.webhook_secret}"
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"https://api.telegram.org/bot{self.token}/setWebhook",
                                params={"url": hook})
                j = r.json()
                self.log("info", f"webhook set → {hook}" if j.get("ok")
                         else f"setWebhook failed: {r.text}")
        except Exception as e:
            self.log("error", f"webhook register failed: {e}")

    # ---------- watchdog ----------
    async def _watchdog_loop(self):
        while self.running:
            await asyncio.sleep(WATCHDOG_TICK)
            if not self.running: break
            if self.mode == "polling" and self.task:
                if self.task.done():
                    exc = None
                    try: exc = self.task.exception()
                    except Exception: pass
                    self.log("warn",
                             f"watchdog: polling task dead (exc={exc!r}) — restarting")
                    if self.auto_restart:
                        await self._auto_restart("watchdog revived dead task")

    # ---------- lifecycle ----------
    async def start(self):
        if self.running: return
        self.dormant = False
        self.load()
        self.running = True
        self.started_at = time.time()
        if self.mode == "polling":
            self.task = asyncio.create_task(self._run_polling())
        else:
            await self._set_webhook()
        if not self.watchdog or self.watchdog.done():
            self.watchdog = asyncio.create_task(self._watchdog_loop())
        self.log("info", f"bot {self.name} started ({self.mode}) · auto-restart ON")

    async def stop(self):
        self.running = False
        if self.watchdog:
            self.watchdog.cancel()
            try: await asyncio.wait_for(self.watchdog, timeout=2)
            except Exception: pass
            self.watchdog = None
        if self.task:
            self.task.cancel()
            try: await asyncio.wait_for(self.task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError): pass
            except Exception: pass
            self.task = None
        if self.mode == "webhook" and self.token:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as c:
                    await c.get(f"https://api.telegram.org/bot{self.token}/deleteWebhook")
            except Exception:
                pass
        self.log("info", f"bot {self.name} stopped")

    async def handle_webhook(self, update):
        if not self.handler:
            self.load()
        await self._safe_handle(update)

# ============================================================
# Supervisor
# ============================================================
class Supervisor:
    def __init__(self):
        self.bots: dict = {}

    async def start(self, row, mode="polling", env=None):
        existing = self.bots.get(row["id"])
        if existing:
            try: await existing.stop()
            except Exception: pass
        b = BotRuntime(row["id"], row["name"], row["token"], row["code"],
                       row["filename"], env=env or {}, mode=mode)
        b.webhook_secret = row.get("webhook_secret", "")
        self.bots[row["id"]] = b
        await b.start()
        return b

    async def stop(self, bot_id):
        b = self.bots.get(bot_id)
        if b:
            await b.stop()

    def get(self, bot_id):
        return self.bots.get(bot_id)

    async def stop_all(self):
        for b in list(self.bots.values()):
            try: await b.stop()
            except Exception: pass

sup = Supervisor()

# ============================================================
# APP RUNTIME — spawn dynamic apps + auto-restart watchdog
# ============================================================
import shlex, socket, signal

APP_RESTART_WINDOW = 300.0
APP_RESTART_MAX    = 5
APP_WATCHDOG_TICK  = 5.0

def _find_free_port(start=8100, end=8999) -> int:
    for p in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0

class AppRuntime:
    def __init__(self, app_id, name, kind, entry, run_cmd, cwd, env=None):
        self.app_id = app_id
        self.name = name
        self.kind = kind or "unknown"
        self.entry = entry or ""
        self.run_cmd = run_cmd or ""
        self.cwd = cwd
        self.env = env or {}
        self.port = _find_free_port()
        self.proc = None
        self.watchdog = None
        self.readers: list = []
        self.logs = deque(maxlen=600)
        self.running = False
        self.dormant = False
        self.started_at = 0.0
        self.restarts = 0
        self.exit_code = None
        self.auto_restart = True
        self._restart_times: list = []
        self._lock = asyncio.Lock()

    def log(self, level, msg):
        entry = {"ts": datetime.utcnow().strftime("%H:%M:%S"),
                 "level": level, "msg": str(msg)}
        self.logs.append(entry)
        # ALSO push to global ring — SSE reads from here
        try:
            applog(self.app_id, level, msg)
        except Exception:
            pass

    def _can_restart(self) -> bool:
        now = time.time()
        self._restart_times = [t for t in self._restart_times if now - t < APP_RESTART_WINDOW]
        return len(self._restart_times) < APP_RESTART_MAX

    def _argv(self):
        cmd = self.run_cmd.strip()
        if not cmd:
            return None
        try:
            parts = shlex.split(cmd)
        except Exception:
            parts = cmd.split()
        if not parts:
            return None
        return parts

    def _proc_env(self):
        env = {**os.environ}
        env["PORT"] = str(self.port)
        env["HOST"] = "127.0.0.1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for k, v in self.env.items():
            if k and v is not None:
                env[str(k)] = str(v)
        return env

    async def _reader(self, stream, tag):
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                txt = line.decode("utf-8", "ignore").rstrip("\r\n")
                if txt:
                    lvl = "error" if tag == "err" else "info"
                    self.log(lvl, txt)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _spawn(self):
        argv = self._argv()
        if not argv:
            self.log("error", f"no run_cmd — kind={self.kind}, entry={self.entry}")
            return False
        cwd = str(self.cwd) if self.cwd else None
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=self._proc_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            self.log("error", f"binary not found: {argv[0]} — {e}")
            return False
        except Exception as e:
            self.log("error", f"spawn failed: {e}")
            return False

        self.readers = [
            asyncio.create_task(self._reader(self.proc.stdout, "out")),
            asyncio.create_task(self._reader(self.proc.stderr, "err")),
        ]
        self.log("info", f"spawned pid={self.proc.pid} port={self.port} — {self.run_cmd}")
        return True

    async def _kill_process(self):
        if not self.proc: return
        if self.proc.returncode is not None: return
        try:
            self.proc.terminate()
        except ProcessLookupError:
            return
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=4)
        except asyncio.TimeoutError:
            try: self.proc.kill()
            except Exception: pass
            try: await asyncio.wait_for(self.proc.wait(), timeout=3)
            except Exception: pass

    async def _restart(self, reason: str = ""):
        if not self.running: return
        async with self._lock:
            if not self.running: return
            if not self._can_restart():
                self.log("error",
                         f"restart limit reached ({APP_RESTART_MAX}/{int(APP_RESTART_WINDOW)}s) — app DORMANT")
                self.dormant = True
                self.running = False
                return
            self._restart_times.append(time.time())
            self.restarts += 1
            self.log("warn", f"restart #{self.restarts} — {reason or 'watchdog'}")
            for t in self.readers:
                t.cancel()
            self.readers = []
            await self._kill_process()
            self.proc = None
            if await self._spawn():
                self.log("info", f"restart complete (#{self.restarts})")
            else:
                self.log("error", "respawn failed — staying down")
                self.running = False

    async def _watchdog(self):
        while self.running:
            await asyncio.sleep(APP_WATCHDOG_TICK)
            if not self.running: break
            if not self.proc:
                continue
            if self.proc.returncode is not None:
                self.exit_code = self.proc.returncode
                self.log("error", f"process exited (code {self.exit_code})")
                if self.auto_restart:
                    await self._restart(f"exit code {self.exit_code}")

    async def start(self):
        if self.running: return False
        self.dormant = False
        self.running = True
        self.started_at = time.time()
        self.port = _find_free_port()
        ok = await self._spawn()
        if not ok:
            self.running = False
            return False
        if not self.watchdog or self.watchdog.done():
            self.watchdog = asyncio.create_task(self._watchdog())
        self.log("info", f"app {self.name} started ({self.kind}) port={self.port} · auto-restart ON")
        return True

    async def stop(self):
        self.running = False
        if self.watchdog:
            self.watchdog.cancel()
            try: await asyncio.wait_for(self.watchdog, timeout=2)
            except Exception: pass
            self.watchdog = None
        for t in self.readers:
            t.cancel()
        self.readers = []
        await self._kill_process()
        self.proc = None
        self.log("info", f"app {self.name} stopped")

    async def request(self, method, path, query, headers, body):
        import httpx
        if not self.proc or self.proc.returncode is not None:
            return None
        url = f"http://127.0.0.1:{self.port}{path}"
        if query:
            url += "?" + query
        hop = {"host", "connection", "content-length", "transfer-encoding",
               "keep-alive", "proxy-authorization", "proxy-authenticate",
               "te", "trailer", "upgrade"}
        clean = {k: v for k, v in headers.items() if k.lower() not in hop}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
                r = await c.request(method, url, headers=clean, content=body)
                return r
        except Exception as e:
            self.log("error", f"proxy error: {e}")
            return None


class _AppSupervisor:
    def __init__(self):
        self.apps: dict = {}

    async def start(self, row, cwd, env=None):
        aid = row["id"]
        existing = self.apps.get(aid)
        if existing:
            try: await existing.stop()
            except Exception: pass
        rt = AppRuntime(aid, row["name"], row.get("type", ""),
                        row.get("entry", ""), row.get("run_cmd", ""),
                        cwd, env=env or {})
        self.apps[aid] = rt
        ok = await rt.start()
        return rt, ok

    async def stop(self, aid):
        rt = self.apps.get(aid)
        if rt:
            await rt.stop()

    def get(self, aid):
        return self.apps.get(aid)

    async def stop_all(self):
        for rt in list(self.apps.values()):
            try: await rt.stop()
            except Exception: pass

appsup = _AppSupervisor()


# ============================================================
# REQUIREMENTS AUTO-INSTALL
# ============================================================
import sys, tempfile

async def install_requirements(requirements_text: str, log_id: str, timeout: int = 420) -> bool:
    """Install a requirements.txt payload. Logs stream to applog(log_id)."""
    text = (requirements_text or "").strip()
    if not text:
        return True
    lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    applog(log_id, "info", f"installing {len(lines)} requirement(s)…")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(text)
            tmp_path = f.name
    except Exception as e:
        applog(log_id, "error", f"tmp file failed: {e}")
        return False

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--no-cache-dir",
            "--disable-pip-version-check", "-r", tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        applog(log_id, "error", f"pip spawn failed: {e}")
        try: os.unlink(tmp_path)
        except Exception: pass
        return False

    try:
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            applog(log_id, "error", f"pip timeout ({timeout}s)")
            return False
        text_out = (out or b"").decode("utf-8", "ignore")
        tail = [l for l in text_out.splitlines() if l.strip()][-25:]
        for l in tail:
            applog(log_id, "info", "pip: " + l)
        if proc.returncode == 0:
            applog(log_id, "info", "requirements installed ✓")
            return True
        applog(log_id, "error", f"pip exited {proc.returncode}")
        return False
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass


def extract_requirements(file_map: dict) -> str:
    """Pick requirements.txt (or pyproject.toml deps) from a zip file_map."""
    for path, data in (file_map or {}).items():
        base = path.split("/")[-1].lower()
        if base in ("requirements.txt", "req.txt"):
            try:
                return data.decode("utf-8", "ignore")
            except Exception:
                return ""
    return ""
