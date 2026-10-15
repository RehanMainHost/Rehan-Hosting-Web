from __future__ import annotations
import io, json, uuid, asyncio, zipfile, os, mimetypes, shutil, subprocess, time, traceback
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends, HTTPException, UploadFile, File
from fastapi.responses import (HTMLResponse, RedirectResponse, JSONResponse,
                               StreamingResponse, Response, FileResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import settings, UPLOAD_DIR, AVATAR_DIR, LOGO_DIR, EDITABLE_KEYS, SECRET_KEYS
import db, auth
from supervisor import (sup, appsup, extract_bundle, applog, applog_snapshot,
                        detect_token, name_from_filename, detect_entry,
                        install_requirements, extract_requirements)

# ============================================================
# constants
# ============================================================
MAX_UPLOAD_ZIP   = 50 * 1024 * 1024
MAX_UPLOAD_IMAGE = 4  * 1024 * 1024
LOGIN_WINDOW     = 300
LOGIN_MAX        = 6

_MIME_EXTRA = {
    ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".woff": "font/woff", ".woff2": "font/woff2", ".json": "application/json",
    ".map": "application/json", ".wasm": "application/wasm",
}

_AVATAR_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_login_attempts: dict = {}

# ============================================================
# boot
# ============================================================
def _migrate_apps_columns():
    try:
        db.fetchone("SELECT entry FROM apps LIMIT 1")
    except Exception:
        for col in ("entry", "run_cmd", "static_root"):
            try: db.execute(f"ALTER TABLE apps ADD COLUMN {col} TEXT")
            except Exception: pass

def _apply_db_settings():
    try:
        rows = db.fetchall("SELECT key, value_enc FROM app_settings")
    except Exception:
        return
    for r in rows:
        k = r["key"]
        if k not in EDITABLE_KEYS: continue
        v = auth.decrypt(r["value_enc"] or "")
        if v:
            try: setattr(settings, k, v)
            except Exception: pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / "apps").mkdir(exist_ok=True)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    LOGO_DIR.mkdir(parents=True, exist_ok=True)

    db.init()
    _migrate_apps_columns()
    try:
        db.migrate_runtime_columns()
    except Exception as _e:
        print(f'[boot] runtime column migration: {_e}')
    _apply_db_settings()
    db.ensure_admin(settings.ADMIN_USER, auth.hash_password(settings.ADMIN_PASSWORD))

    # boot recovery: start ALL bots that have a token (auto-start always on)
    try:
        rows = db.fetchall("SELECT * FROM bots")
        for row in rows:
            try:
                tok = auth.decrypt(row["token_enc"] or "")
                if not tok:
                    db.execute("UPDATE bots SET status='error' WHERE id=?", (row["id"],))
                    print(f"[boot] bot {row.get('name')} — no token, skipped")
                    continue
                row["token"] = tok
                reqs = (row.get("requirements") or "").strip()
                if reqs:
                    import hashlib as _hl
                    cur_hash = _hl.sha256(reqs.encode()).hexdigest()[:16]
                    if cur_hash != (row.get("deps_hash") or ""):
                        print(f"[boot] bot {row.get('name')} — installing deps…")
                        ok = await install_requirements(reqs, row["id"])
                        if ok:
                            db.execute("UPDATE bots SET deps_hash=? WHERE id=?", (cur_hash, row["id"]))
                        else:
                            db.execute("UPDATE bots SET status='error' WHERE id=?", (row["id"],))
                            continue
                env = _load_env("bot", row["id"])
                mode = "webhook" if settings.PUBLIC_URL else "polling"
                await sup.start(row, mode=mode, env=env)
                db.execute("UPDATE bots SET status='running' WHERE id=?", (row["id"],))
                print(f"[boot] bot {row.get('name')} auto-started")
            except Exception as e:
                print(f"[boot] bot {row.get('id')} restore failed: {e}")
                try:
                    db.execute("UPDATE bots SET status='error' WHERE id=?", (row["id"],))
                except Exception:
                    pass
    except Exception as e:
        print(f"[boot] bot restore scan failed: {e}")

    # boot recovery: start ALL dynamic apps (static/php stay in-process)
    try:
        rows = db.fetchall("SELECT * FROM apps")
        for row in rows:
            try:
                kind = (row.get("type") or "").lower()
                if kind in ("static", "php"):
                    db.execute("UPDATE apps SET status='hosted' WHERE id=?", (row["id"],))
                    continue
                d = _app_dir(row["id"])
                if not d.exists():
                    continue
                reqs = (row.get("requirements") or "").strip()
                if reqs:
                    import hashlib as _hl
                    cur_hash = _hl.sha256(reqs.encode()).hexdigest()[:16]
                    if cur_hash != (row.get("deps_hash") or ""):
                        print(f"[boot] app {row.get('name')} — installing deps…")
                        ok = await install_requirements(reqs, row["id"])
                        if ok:
                            db.execute("UPDATE apps SET deps_hash=? WHERE id=?", (cur_hash, row["id"]))
                        else:
                            db.execute("UPDATE apps SET status='error' WHERE id=?", (row["id"],))
                            continue
                env = _load_env("app", row["id"])
                rt, ok = await appsup.start(row, d, env=env)
                if ok:
                    db.execute("UPDATE apps SET status='running' WHERE id=?", (row["id"],))
                    print(f"[boot] app {row.get('name')} auto-started")
                else:
                    db.execute("UPDATE apps SET status='error' WHERE id=?", (row["id"],))
            except Exception as e:
                print(f"[boot] app {row.get('id')} restore failed: {e}")
    except Exception as e:
        print(f"[boot] app restore scan failed: {e}")

    yield

    try:
        await sup.stop_all()
    except Exception:
        pass
    try:
        await appsup.stop_all()
    except Exception:
        pass

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ============================================================
# helpers
# ============================================================
def current_user(request: Request):
    tok = request.cookies.get(settings.SESSION_COOKIE)
    return auth.read_session(tok) if tok else None

def require_user(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401)
    return u

@app.exception_handler(HTTPException)
async def _exc(request: Request, exc: HTTPException):
    if exc.status_code == 401 and "text/html" in (request.headers.get("accept") or ""):
        return RedirectResponse("/login", status_code=303)
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

def _get_avatar_url(username):
    if not username: return None
    for e in _AVATAR_EXTS:
        if (AVATAR_DIR / f"{username}{e}").exists():
            return f"/avatar/{username}"
    return None

def _get_logo_url():
    for e in _AVATAR_EXTS:
        if (LOGO_DIR / f"logo{e}").exists():
            return f"/branding/logo{e}"
    return None

def render(name, request, **ctx):
    u = current_user(request)
    ctx.setdefault("user", u)
    ctx.setdefault("app_name", settings.APP_NAME)
    ctx.setdefault("page", "")
    ctx.setdefault("avatar_url", _get_avatar_url(u))
    ctx.setdefault("logo_url", _get_logo_url())
    ctx.update(request=request)
    return templates.TemplateResponse(name, ctx)

def _load_env(owner_type: str, owner_id: str) -> dict:
    rows = db.fetchall(
        "SELECT key, value_enc FROM env_vars WHERE owner_type=? AND owner_id=?",
        (owner_type, owner_id))
    return {r["key"]: auth.decrypt(r["value_enc"]) for r in rows}

def _save_env(owner_type: str, owner_id: str, env: dict):
    for k, v in env.items():
        if not k: continue
        existing = db.fetchone(
            "SELECT id FROM env_vars WHERE owner_type=? AND owner_id=? AND key=?",
            (owner_type, owner_id, k))
        if existing:
            db.execute("UPDATE env_vars SET value_enc=? WHERE id=?",
                       (auth.encrypt(v), existing["id"]))
        else:
            db.execute(
                "INSERT INTO env_vars (id, owner_type, owner_id, key, value_enc) VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex[:12], owner_type, owner_id, k, auth.encrypt(v)))

def _delete_env(owner_type: str, owner_id: str, key: str):
    db.execute("DELETE FROM env_vars WHERE owner_type=? AND owner_id=? AND key=?",
               (owner_type, owner_id, key))

def mask(v: str, keep: int = 4) -> str:
    if not v: return ""
    if len(v) <= keep * 2: return "•" * len(v)
    return v[:keep] + "•" * 8 + v[-keep:]

def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _check_login_limit(ip: str) -> bool:
    now = time.time()
    arr = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW]
    _login_attempts[ip] = arr
    # periodic purge — keeps dict bounded
    if len(_login_attempts) > 500:
        for k in list(_login_attempts.keys()):
            _login_attempts[k] = [t for t in _login_attempts[k] if now - t < LOGIN_WINDOW]
            if not _login_attempts[k]:
                _login_attempts.pop(k, None)
    return len(arr) < LOGIN_MAX

def _record_login_attempt(ip: str):
    _login_attempts.setdefault(ip, []).append(time.time())

def _cookie_secure() -> bool:
    return settings.PUBLIC_URL.startswith("https://")

async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, f"file too large (max {max_bytes // 1024 // 1024}MB)")
    return data

def _app_dir(aid: str) -> Path:
    return UPLOAD_DIR / "apps" / aid

# ============================================================
# auth
# ============================================================
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", 303)
    return render("login.html", request, err=None)

@app.post("/login")
async def login_submit(request: Request,
                       username: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request)
    if not _check_login_limit(ip):
        return render("login.html", request,
                      err="Too many attempts — wait 5 minutes.")
    row = db.fetchone("SELECT * FROM users WHERE username=?", (username,))
    if row and auth.verify_password(password, row["password_hash"]):
        _login_attempts.pop(ip, None)
        resp = RedirectResponse("/", 303)
        resp.set_cookie(settings.SESSION_COOKIE, auth.make_session(username),
                        httponly=True, samesite="lax",
                        secure=_cookie_secure(),
                        max_age=settings.SESSION_MAX_AGE)
        return resp
    _record_login_attempt(ip)
    return render("login.html", request, err="Wrong credentials")

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(settings.SESSION_COOKIE)
    return resp

# ============================================================
# dashboard
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user=Depends(require_user)):
    apps = db.fetchall("SELECT * FROM apps WHERE user_id=? ORDER BY created_at DESC", (user,))
    bots = db.fetchall("SELECT id,name,filename,status,created_at FROM bots WHERE user_id=? ORDER BY created_at DESC", (user,))
    for b in bots:
        rt = sup.get(b["id"])
        b["live"] = bool(rt and rt.running)
    stats = {
        "apps": len(apps),
        "bots": len(bots),
        "running": sum(1 for b in bots if b["live"]),
        "db_mode": settings.DB_MODE,
    }
    return render("dashboard.html", request, page="dashboard",
                  apps=apps, bots=bots, stats=stats)


# ============================================================
# background install + autostart helpers
# ============================================================
async def _bg_install_then_start_bot(bid: str):
    row = db.fetchone("SELECT * FROM bots WHERE id=?", (bid,))
    if not row:
        return
    reqs = (row.get("requirements") or "").strip()
    if reqs:
        import hashlib as _hl
        cur_hash = _hl.sha256(reqs.encode()).hexdigest()[:16]
        if cur_hash != (row.get("deps_hash") or ""):
            ok = await install_requirements(reqs, bid)
            if not ok:
                db.execute("UPDATE bots SET status='error' WHERE id=?", (bid,))
                applog(bid, "error", "install fail — bot start hoyni")
                return
            db.execute("UPDATE bots SET deps_hash=? WHERE id=?", (cur_hash, bid))
    # now actually start
    row = db.fetchone("SELECT * FROM bots WHERE id=?", (bid,))
    if not row:
        return
    row["token"] = auth.decrypt(row["token_enc"] or "")
    if not row["token"]:
        db.execute("UPDATE bots SET status='error' WHERE id=?", (bid,))
        applog(bid, "error", "no token — start korte parchi na")
        return
    env = _load_env("bot", bid)
    mode = "webhook" if settings.PUBLIC_URL else "polling"
    try:
        await sup.start(row, mode=mode, env=env)
        db.execute("UPDATE bots SET status='running' WHERE id=?", (bid,))
        applog(bid, "info", "✓ bot started")
    except Exception as e:
        db.execute("UPDATE bots SET status='error' WHERE id=?", (bid,))
        applog(bid, "error", f"start failed: {e}")


async def _bg_install_then_start_app(aid: str):
    row = db.fetchone("SELECT * FROM apps WHERE id=?", (aid,))
    if not row:
        return
    kind = (row.get("type") or "").lower()
    if kind in ("static", "php"):
        return
    reqs = (row.get("requirements") or "").strip()
    if reqs:
        import hashlib as _hl
        cur_hash = _hl.sha256(reqs.encode()).hexdigest()[:16]
        if cur_hash != (row.get("deps_hash") or ""):
            ok = await install_requirements(reqs, aid)
            if not ok:
                db.execute("UPDATE apps SET status='error' WHERE id=?", (aid,))
                applog(aid, "error", "install fail — app start hoyni")
                return
            db.execute("UPDATE apps SET deps_hash=? WHERE id=?", (cur_hash, aid))
    d = _app_dir(aid)
    if not d.exists():
        return
    env = _load_env("app", aid)
    try:
        rt, ok = await appsup.start(row, d, env=env)
        if ok:
            db.execute("UPDATE apps SET status='running' WHERE id=?", (aid,))
            applog(aid, "info", "✓ app started")
        else:
            db.execute("UPDATE apps SET status='error' WHERE id=?", (aid,))
    except Exception as e:
        db.execute("UPDATE apps SET status='error' WHERE id=?", (aid,))
        applog(aid, "error", f"start failed: {e}")


# ============================================================
# bots
# ============================================================
@app.get("/bots", response_class=HTMLResponse)
async def bots_list(request: Request, user=Depends(require_user)):
    rows = db.fetchall("SELECT id,name,filename,status,created_at FROM bots WHERE user_id=? ORDER BY created_at DESC", (user,))
    for r in rows:
        rt = sup.get(r["id"])
        r["live"] = bool(rt and rt.running)
        r["restarts"] = int(getattr(rt, "restarts", 0)) if rt else 0
    return render("bots.html", request, page="bots", bots=rows, bot=None,
                  live=False, dormant=False, restarts=0, env={})

@app.get("/bots/{bid}", response_class=HTMLResponse)
async def bot_detail(bid: str, request: Request, user=Depends(require_user)):
    row = db.fetchone("SELECT * FROM bots WHERE id=? AND user_id=?", (bid, user))
    if not row: raise HTTPException(404, "bot not found")
    rt = sup.get(bid)
    live = bool(rt and rt.running)
    dormant = bool(rt and getattr(rt, "dormant", False))
    restarts = int(getattr(rt, "restarts", 0)) if rt else 0
    env = _load_env("bot", bid)
    return render("bots.html", request, page="bots", bots=None, bot=row,
                  live=live, dormant=dormant, restarts=restarts, env=env,
                  auto_start=int(row.get("auto_start") or 0))

@app.post("/bots")
async def bot_create(request: Request, user=Depends(require_user),
                     name: str = Form(""), token: str = Form(""),
                     code: str = Form(""),
                     file: UploadFile = File(None)):
    bid = uuid.uuid4().hex[:12]
    filename = "bot.py"
    detected_env = {}
    uploaded_name = ""

    if file is not None and file.filename:
        raw = await _read_capped(file, MAX_UPLOAD_ZIP)
        uploaded_name = file.filename
        if file.filename.lower().endswith(".zip"):
            entry_src, entry_name, detected_env, _all = extract_bundle(raw)
            if entry_src:
                code = entry_src
                filename = os.path.basename(entry_name) or "bot.py"
        else:
            try:
                code = raw.decode("utf-8")
            except UnicodeDecodeError:
                code = raw.decode("latin-1")
            filename = file.filename

    if not token.strip():
        token = detect_token(detected_env, code)

    if not name.strip():
        if uploaded_name:
            name = name_from_filename(uploaded_name)
        elif filename and filename != "bot.py":
            name = name_from_filename(filename)
        else:
            name = f"bot-{bid[:6]}"

    if not code.strip():
        code = ('# {name}\n'
                'async def handle_update(update):\n'
                '    log("got update: " + str(update.get("update_id")))\n').format(name=name)

    reqs = ""
    if file is not None and file.filename and file.filename.lower().endswith(".zip"):
        try:
            _src, _nm, _env2, _files = extract_bundle(raw)
            reqs = extract_requirements(_files)
        except Exception:
            reqs = ""

    db.execute(
        "INSERT INTO bots (id,user_id,name,token_enc,code,filename,webhook_secret,status,created_at,requirements) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (bid, user, name, auth.encrypt(token or ""), code, filename,
         uuid.uuid4().hex, "stopped", datetime.utcnow().isoformat(), reqs))
    if reqs:
        applog(bid, "info", f"detected requirements.txt ({len(reqs.splitlines())} lines) — install hobe Start e")

    if detected_env:
        _save_env("bot", bid, detected_env)
    if token and "BOT_TOKEN" not in detected_env:
        _save_env("bot", bid, {"BOT_TOKEN": token})

    applog(f"bot-{bid}", "info",
           f"created · name={name} · token={'auto' if token else 'missing'} · env={len(detected_env)}")
    return RedirectResponse(f"/bots/{bid}", 303)


@app.post("/bots/{bid}/autostart")
async def bot_autostart_toggle(bid: str, request: Request, user=Depends(require_user)):
    row = db.fetchone("SELECT auto_start FROM bots WHERE id=? AND user_id=?", (bid, user))
    if not row: raise HTTPException(404)
    cur = int(row.get("auto_start") or 0)
    new = 0 if cur else 1
    db.execute("UPDATE bots SET auto_start=? WHERE id=?", (new, bid))
    applog(bid, "info", f"auto_start = {'ON' if new else 'OFF'}")
    return RedirectResponse(f"/bots/{bid}", 303)

@app.post("/bots/{bid}/env")
async def bot_env_add(bid: str, request: Request, user=Depends(require_user),
                      key: str = Form(...), value: str = Form("")):
    row = db.fetchone("SELECT id FROM bots WHERE id=? AND user_id=?", (bid, user))
    if not row: raise HTTPException(404)
    _save_env("bot", bid, {key.strip(): value})
    return RedirectResponse(f"/bots/{bid}", 303)

@app.post("/bots/{bid}/env/delete")
async def bot_env_del(bid: str, request: Request, user=Depends(require_user),
                      key: str = Form(...)):
    row = db.fetchone("SELECT id FROM bots WHERE id=? AND user_id=?", (bid, user))
    if not row: raise HTTPException(404)
    _delete_env("bot", bid, key)
    return RedirectResponse(f"/bots/{bid}", 303)

@app.post("/bots/{bid}/start")
async def bot_start(bid: str, request: Request, user=Depends(require_user)):
    row = db.fetchone("SELECT id FROM bots WHERE id=? AND user_id=?", (bid, user))
    if not row: raise HTTPException(404)
    db.execute("UPDATE bots SET status='starting' WHERE id=?", (bid,))
    applog(bid, "info", "start requested — background e cholche")
    asyncio.create_task(_bg_install_then_start_bot(bid))
    return RedirectResponse(f"/bots/{bid}", 303)

@app.post("/bots/{bid}/stop")
async def bot_stop(bid: str, request: Request, user=Depends(require_user)):
    row = db.fetchone("SELECT id FROM bots WHERE id=? AND user_id=?", (bid, user))
    if not row: raise HTTPException(404)
    await sup.stop(bid)
    db.execute("UPDATE bots SET status='stopped' WHERE id=?", (bid,))
    return RedirectResponse(f"/bots/{bid}", 303)

@app.post("/bots/{bid}/delete")
async def bot_delete(bid: str, request: Request, user=Depends(require_user)):
    await sup.stop(bid)
    db.execute("DELETE FROM bots WHERE id=? AND user_id=?", (bid, user))
    db.execute("DELETE FROM env_vars WHERE owner_type='bot' AND owner_id=?", (bid,))
    return RedirectResponse("/bots", 303)

@app.post("/webhook/{bid}/{secret}")
async def bot_webhook(bid: str, secret: str, request: Request):
    row = db.fetchone("SELECT webhook_secret FROM bots WHERE id=?", (bid,))
    if not row or row["webhook_secret"] != secret:
        raise HTTPException(403, "bad secret")
    rt = sup.get(bid)
    if not rt: raise HTTPException(503, "bot not running")
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")
    try:
        await rt.handle_webhook(update)
    except Exception as e:
        rt.log("error", f"handler crashed: {e}")
    return {"ok": True}

# ============================================================
# SSE — unified stream (heartbeat + batch)
# ============================================================
@app.get("/stream")
async def stream_all(request: Request, user=Depends(require_user)):
    async def gen():
        seen_bot: dict = {}
        seen_app: dict = {}
        seen_bot_ring: dict = {}
        for b in db.fetchall("SELECT id FROM bots WHERE user_id=?", (user,)):
            rt = sup.get(b["id"])
            if rt: seen_bot[b["id"]] = len(rt.logs)
        last_ping = time.time()
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                bots = db.fetchall("SELECT id FROM bots WHERE user_id=?", (user,))
            except Exception:
                bots = []
            seen_bot_ring = seen_bot_ring if 'seen_bot_ring' in dir() else {}
            for b in bots:
                bid = b["id"]
                rt = sup.get(bid)
                lines = list(rt.logs) if rt else []
                cur = seen_bot.get(bid, 0)
                if len(lines) > cur:
                    for l in lines[cur:]:
                        payload = {"kind": "bot", "id": bid, **l}
                        yield f"data: {json.dumps(payload)}\n\n"
                    seen_bot[bid] = len(lines)
                ring = applog_snapshot(bid)
                cur2 = seen_bot_ring.get(bid, 0)
                if len(ring) > cur2:
                    for l in ring[cur2:]:
                        payload = {"kind": "bot", "id": bid, **l}
                        yield f"data: {json.dumps(payload)}\n\n"
                    seen_bot_ring[bid] = len(ring)

            try:
                apps_now = db.fetchall("SELECT id FROM apps WHERE user_id=?", (user,))
            except Exception:
                apps_now = []
            for a in apps_now:
                aid = a["id"]
                cur = seen_app.get(aid, 0)
                lines = applog_snapshot(aid)
                if len(lines) > cur:
                    for l in lines[cur:]:
                        payload = {"kind": "app", "id": aid, **l}
                        yield f"data: {json.dumps(payload)}\n\n"
                    seen_app[aid] = len(lines)

            if time.time() - last_ping > 15:
                yield ": ping\n\n"
                last_ping = time.time()
            await asyncio.sleep(1.0)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})

# ============================================================
# apps
# ============================================================
@app.get("/apps", response_class=HTMLResponse)
async def apps_list(request: Request, user=Depends(require_user)):
    rows = db.fetchall("SELECT * FROM apps WHERE user_id=? ORDER BY created_at DESC", (user,))
    for r in rows:
        d = _app_dir(r["id"])
        r["file_count"] = sum(1 for _ in d.rglob("*") if _.is_file()) if d.exists() else 0
        rt = appsup.get(r["id"])
        r["live"] = bool(rt and rt.running and rt.proc and rt.proc.returncode is None)
        r["restarts"] = int(getattr(rt, "restarts", 0)) if rt else 0
    return render("apps.html", request, page="apps", apps=rows, app=None, env={},
                  files=[], live=False, dormant=False, restarts=0, port=None, is_dynamic=False)

@app.get("/apps/{aid}", response_class=HTMLResponse)
async def app_detail(aid: str, request: Request, user=Depends(require_user)):
    row = db.fetchone("SELECT * FROM apps WHERE id=? AND user_id=?", (aid, user))
    if not row: raise HTTPException(404)
    env = _load_env("app", aid)
    d = _app_dir(aid)
    files = []
    if d.exists():
        for f in d.rglob("*"):
            if f.is_file():
                files.append(str(f.relative_to(d)))
        files = files[:200]
    rt = appsup.get(aid)
    live = bool(rt and rt.running and rt.proc and rt.proc.returncode is None)
    dormant = bool(rt and getattr(rt, "dormant", False))
    restarts = int(getattr(rt, "restarts", 0)) if rt else 0
    port = getattr(rt, "port", None) if rt else None
    is_dynamic = (row.get("type") or "").lower() not in ("static", "php")
    return render("apps.html", request, page="apps", apps=None, app=row, env=env, files=files,
                  live=live, dormant=dormant, restarts=restarts, port=port, is_dynamic=is_dynamic,
                  auto_start=int(row.get("auto_start") or 0))

@app.post("/apps")
async def app_create(request: Request, user=Depends(require_user),
                     name: str = Form(""), type: str = Form(""),
                     repo: str = Form(""), service_id: str = Form(""),
                     url: str = Form(""),
                     file: UploadFile = File(None)):
    aid = uuid.uuid4().hex[:12]
    status = "registered"
    detected_env = {}
    file_count = 0
    entry_info = {"entry": "", "kind": "unknown", "run_cmd": "", "static_root": ""}
    uploaded_name = ""

    if file is not None and file.filename:
        raw = await _read_capped(file, MAX_UPLOAD_ZIP)
        uploaded_name = file.filename
        if file.filename.lower().endswith(".zip"):
            dest = _app_dir(aid)
            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
                tops = set(n.split("/")[0] for n in names if "/" in n)
                strip = (list(tops)[0] + "/") if (len(tops) == 1 and all("/" in n for n in names)) else ""
                file_map = {}
                for n in names:
                    if "__MACOSX" in n: continue
                    rel = n[len(strip):] if strip and n.startswith(strip) else n
                    if not rel or rel.endswith("/"): continue
                    target = dest / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    data = z.read(n)
                    target.write_bytes(data)
                    file_map[rel] = data
                    file_count += 1

            entry_info = detect_entry(file_map)

            from supervisor import _parse_env_text, _parse_json_config, _parse_python_config
            for rel, data in file_map.items():
                nm = rel.split("/")[-1].lower()
                try:
                    txt = data.decode("utf-8", "ignore")
                except Exception:
                    continue
                if nm in (".env", ".env.example", "env.txt"):
                    detected_env.update(_parse_env_text(txt))
                elif nm in ("config.json", "settings.json"):
                    detected_env.update(_parse_json_config(txt))
                elif nm in ("config.py", "settings.py", "conf.py"):
                    detected_env.update(_parse_python_config(txt))

            status = "hosted"
            if not url:
                base = settings.PUBLIC_URL or f"http://127.0.0.1:{settings.PORT}"
                url = f"{base}/site/{aid}/"

    if not name.strip():
        name = name_from_filename(uploaded_name) if uploaded_name else f"app-{aid[:6]}"

    if not type.strip() or type == "auto":
        mapping = {
            "static": "static", "node": "web", "flask": "web", "django": "web",
            "python": "web", "python-wsgi": "web", "procfile": "web",
            "php": "php", "go": "web", "unknown": "static",
        }
        type = mapping.get(entry_info["kind"], "static")

    app_reqs = ""
    if file is not None and file.filename and file.filename.lower().endswith(".zip"):
        try:
            app_reqs = extract_requirements(file_map)
        except Exception:
            app_reqs = ""

    db.execute(
        "INSERT INTO apps (id,user_id,name,type,repo,service_id,url,status,created_at,"
        "entry,run_cmd,static_root,requirements) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, user, name, type, repo, service_id, url, status,
         datetime.utcnow().isoformat(),
         entry_info["entry"], entry_info["run_cmd"], entry_info["static_root"],
         app_reqs))
    if app_reqs:
        applog(aid, "info", f"detected requirements.txt ({len(app_reqs.splitlines())} lines) — install hobe Start e")

    if detected_env:
        _save_env("app", aid, detected_env)

    applog(aid, "info",
           f"registered · {file_count} files · kind={entry_info['kind']} · entry={entry_info['entry'] or '—'} · env={len(detected_env)}")
    return RedirectResponse(f"/apps/{aid}", 303)


@app.post("/apps/{aid}/autostart")
async def app_autostart_toggle(aid: str, request: Request, user=Depends(require_user)):
    row = db.fetchone("SELECT auto_start FROM apps WHERE id=? AND user_id=?", (aid, user))
    if not row: raise HTTPException(404)
    cur = int(row.get("auto_start") or 0)
    new = 0 if cur else 1
    db.execute("UPDATE apps SET auto_start=? WHERE id=?", (new, aid))
    applog(aid, "info", f"auto_start = {'ON' if new else 'OFF'}")
    return RedirectResponse(f"/apps/{aid}", 303)

@app.post("/apps/{aid}/env")
async def app_env_add(aid: str, request: Request, user=Depends(require_user),
                      key: str = Form(...), value: str = Form("")):
    row = db.fetchone("SELECT id FROM apps WHERE id=? AND user_id=?", (aid, user))
    if not row: raise HTTPException(404)
    _save_env("app", aid, {key.strip(): value})
    applog(aid, "info", f"env set: {key.strip()}")
    return RedirectResponse(f"/apps/{aid}", 303)

@app.post("/apps/{aid}/env/delete")
async def app_env_del(aid: str, request: Request, user=Depends(require_user),
                      key: str = Form(...)):
    row = db.fetchone("SELECT id FROM apps WHERE id=? AND user_id=?", (aid, user))
    if not row: raise HTTPException(404)
    _delete_env("app", aid, key)
    applog(aid, "info", f"env removed: {key}")
    return RedirectResponse(f"/apps/{aid}", 303)

@app.post("/apps/{aid}/delete")
async def app_delete(aid: str, request: Request, user=Depends(require_user)):
    row = db.fetchone("SELECT id FROM apps WHERE id=? AND user_id=?", (aid, user))
    if not row: raise HTTPException(404)
    d = _app_dir(aid)
    if d.exists(): shutil.rmtree(d, ignore_errors=True)
    db.execute("DELETE FROM apps WHERE id=? AND user_id=?", (aid, user))
    db.execute("DELETE FROM env_vars WHERE owner_type='app' AND owner_id=?", (aid,))
    return RedirectResponse("/apps", 303)

# ============================================================
# app lifecycle (dynamic apps — spawn + auto-restart)
# ============================================================
_STATIC_KINDS = {"static"}
_PHP_KINDS    = {"php"}

def _is_dynamic(app_row) -> bool:
    kind = (app_row.get("type") or "").lower()
    return kind not in _STATIC_KINDS and kind not in _PHP_KINDS

@app.post("/apps/{aid}/start")
async def app_start(aid: str, request: Request, user=Depends(require_user)):
    row = db.fetchone("SELECT id,type FROM apps WHERE id=? AND user_id=?", (aid, user))
    if not row: raise HTTPException(404)
    if (row.get("type") or "").lower() in ("static", "php"):
        applog(aid, "warn", f"kind={row.get('type')} — kono process nai, always served")
        return RedirectResponse(f"/apps/{aid}", 303)
    d = _app_dir(aid)
    if not d.exists():
        applog(aid, "error", "no files — reupload kor")
        return RedirectResponse(f"/apps/{aid}", 303)
    db.execute("UPDATE apps SET status='starting' WHERE id=?", (aid,))
    applog(aid, "info", "start requested — background e cholche")
    asyncio.create_task(_bg_install_then_start_app(aid))
    return RedirectResponse(f"/apps/{aid}", 303)

@app.post("/apps/{aid}/stop")
async def app_stop(aid: str, request: Request, user=Depends(require_user)):
    row = db.fetchone("SELECT id FROM apps WHERE id=? AND user_id=?", (aid, user))
    if not row: raise HTTPException(404)
    await appsup.stop(aid)
    db.execute("UPDATE apps SET status='stopped' WHERE id=?", (aid,))
    applog(aid, "info", "stopped by user")
    return RedirectResponse(f"/apps/{aid}", 303)

@app.post("/apps/{aid}/restart")
async def app_restart(aid: str, request: Request, user=Depends(require_user)):
    row = db.fetchone("SELECT * FROM apps WHERE id=? AND user_id=?", (aid, user))
    if not row: raise HTTPException(404)
    if not _is_dynamic(row):
        return RedirectResponse(f"/apps/{aid}", 303)
    d = _app_dir(aid)
    env = _load_env("app", aid)
    rt, ok = await appsup.start(row, d, env=env)
    applog(aid, "info", f"manual restart ({'ok' if ok else 'fail'})")
    db.execute(f"UPDATE apps SET status='{'running' if ok else 'error'}' WHERE id=?", (aid,))
    return RedirectResponse(f"/apps/{aid}", 303)

# ============================================================
# app proxy — /site/<id>/ → dynamic app process
# ============================================================
@app.api_route("/app-proxy/{aid}/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def app_proxy(aid: str, path: str, request: Request):
    rt = appsup.get(aid)
    if not rt or not rt.proc or rt.proc.returncode is not None:
        return Response(_not_found_body(path), status_code=503,
                        media_type="text/html")
    body = await request.body()
    query = request.url.query or ""
    target_path = "/" + path if path else "/"
    r = await rt.request(request.method, target_path, query,
                         dict(request.headers), body)
    if r is None:
        return Response(_not_found_body(path), status_code=502,
                        media_type="text/html")
    hop = {"content-encoding", "content-length", "transfer-encoding",
           "connection", "keep-alive"}
    out_headers = {k: v for k, v in r.headers.items() if k.lower() not in hop}
    return Response(content=r.content, status_code=r.status_code, headers=out_headers)

# ============================================================
# php runtime
# ============================================================
def _php_binary():
    for b in ("php-cgi", "php-cgi8", "php-cgi83", "php-cgi82", "php-cgi81"):
        p = shutil.which(b)
        if p: return p
    return None

def _php_tmp() -> str:
    prefix = os.environ.get("PREFIX", "/tmp")
    t = os.path.join(prefix, "tmp")
    return t if os.path.isdir(t) else "/tmp"

async def _run_php(request: Request, script: Path, root: Path):
    php = _php_binary()
    if not php:
        raise HTTPException(500, "php runtime missing — pkg install php")
    if request.method in ("HEAD", "OPTIONS", "TRACE", "CONNECT"):
        # still run for HEAD (headers matter), strip body below
        pass
    try:
        body = await request.body()
    except Exception:
        body = b""
    try:
        rel = script.relative_to(root)
    except Exception:
        rel = Path(script.name)
    tmp = _php_tmp()
    env = {**os.environ}
    env.update({
        "REDIRECT_STATUS": "1",
        "GATEWAY_INTERFACE": "CGI/1.1",
        "REQUEST_METHOD": request.method,
        "QUERY_STRING": request.url.query or "",
        "SCRIPT_FILENAME": str(script),
        "SCRIPT_NAME": "/" + str(rel).replace("\\", "/"),
        "DOCUMENT_ROOT": str(root),
        "REQUEST_URI": str(request.url.path) + (("?" + request.url.query) if request.url.query else ""),
        "SERVER_PROTOCOL": "HTTP/1.1",
        "SERVER_SOFTWARE": "RehanHost",
        "SERVER_NAME": request.url.hostname or "127.0.0.1",
        "SERVER_PORT": str(request.url.port or settings.PORT),
        "REMOTE_ADDR": (request.client.host if request.client else "127.0.0.1"),
        "CONTENT_TYPE": request.headers.get("content-type", ""),
        "CONTENT_LENGTH": str(len(body)),
        "PATH_INFO": "",
        "PATH_TRANSLATED": str(script),
    })
    for k, v in request.headers.items():
        key = "HTTP_" + k.upper().replace("-", "_")
        if key in ("HTTP_CONTENT_TYPE", "HTTP_CONTENT_LENGTH"): continue
        env[key] = v
    argv = [php, "-d", f"session.save_path={tmp}", "-d", f"upload_tmp_dir={tmp}"]
    try:
        proc = subprocess.run(argv, input=body, capture_output=True, env=env, timeout=30)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "php timeout")
    except Exception as e:
        raise HTTPException(500, f"php exec failed: {e}")
    raw = proc.stdout or b""
    if not raw and proc.stderr:
        raise HTTPException(500, "php error: " + proc.stderr.decode("utf-8", "ignore")[:500])
    sep = raw.find(b"\r\n\r\n")
    if sep >= 0:
        head, out_body = raw[:sep], raw[sep + 4:]
    else:
        sep = raw.find(b"\n\n")
        if sep >= 0:
            head, out_body = raw[:sep], raw[sep + 2:]
        else:
            head, out_body = b"", raw
    status = 200
    headers = {}
    cookies = []
    for line in head.decode("latin-1").splitlines():
        if not line.strip(): continue
        if line.startswith("HTTP/"):
            try: status = int(line.split()[1])
            except Exception: pass
            continue
        if ":" not in line: continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        lk = k.lower()
        if lk == "status":
            try: status = int(v.split()[0])
            except Exception: pass
        elif lk == "set-cookie":
            cookies.append(v)
        else:
            headers[k] = v
    resp = Response(content=out_body, status_code=status, headers=headers)
    for c in cookies:
        resp.headers.append("set-cookie", c)
    return resp

# ============================================================
# serve hosted app files (PUBLIC — no auth)
# ============================================================
def _not_found_body(path: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>404 — Not Found</title>
<style>
body{{background:#0d0d0f;color:#e8e8ea;font-family:system-ui,sans-serif;
     display:grid;place-items:center;height:100vh;margin:0;text-align:center}}
h1{{font-size:52px;color:#d4af37;margin:0 0 8px}}
p{{color:#8a8a92;margin:4px 0}}
code{{background:#1b1b21;padding:2px 6px;border-radius:5px;font-size:12px}}
</style></head><body><div>
<h1>404</h1><p>No file at <code>{path}</code></p>
</div></body></html>"""

@app.get("/site/{aid}")
async def site_root(aid: str, request: Request):
    return await serve_site(aid, "", request)

@app.get("/site/{aid}/{path:path}")
async def serve_site(aid: str, path: str, request: Request):
    row = db.fetchone("SELECT id,type FROM apps WHERE id=?", (aid,))
    if not row:
        return Response(_not_found_body(path), status_code=404, media_type="text/html")

    # dynamic app running → proxy through to its process
    kind = (row.get("type") or "").lower()
    if kind not in ("static", "php"):
        rt = appsup.get(aid)
        if rt and rt.running and rt.proc and rt.proc.returncode is None:
            q = request.url.query or ""
            tp = "/" + path if path else "/"
            try:
                body = await request.body()
            except Exception:
                body = b""
            r = await rt.request(request.method, tp, q, dict(request.headers), body)
            if r is not None:
                hop = {"content-encoding", "content-length", "transfer-encoding",
                       "connection", "keep-alive"}
                out_headers = {k: v for k, v in r.headers.items() if k.lower() not in hop}
                return Response(content=r.content, status_code=r.status_code,
                                headers=out_headers)

    root = _app_dir(aid)
    if not root.exists():
        return Response(_not_found_body(path), status_code=404, media_type="text/html")

    target = (root / path).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return Response("bad path", status_code=403)

    # directory → index.php → index.html
    if target.is_dir():
        php_idx = target / "index.php"
        if php_idx.exists():
            return await _run_php(request, php_idx, root)
        target = target / "index.html"

    # file match
    if target.exists() and target.is_file():
        if target.suffix.lower() == ".php":
            return await _run_php(request, target, root)
        mime = _MIME_EXTRA.get(target.suffix.lower())
        if not mime:
            mime, _ = mimetypes.guess_type(str(target))
        return FileResponse(target, media_type=mime or "application/octet-stream")

    # fallback: root index.php with PATH_INFO
    php_root = root / "index.php"
    if php_root.exists():
        return await _run_php(request, php_root, root)

    # fallback: root index.html (SPA)
    html_root = root / "index.html"
    if html_root.exists() and "." not in Path(path).name:
        return FileResponse(html_root)

    # 404 page
    custom_404 = root / "404.html"
    if custom_404.exists():
        return FileResponse(custom_404, status_code=404)
    return Response(_not_found_body(path), status_code=404, media_type="text/html")

# ============================================================
# public assets
# ============================================================
@app.get("/avatar/{uname}")
async def avatar_serve(uname: str):
    for e in _AVATAR_EXTS:
        p = AVATAR_DIR / f"{uname}{e}"
        if p.exists():
            return FileResponse(p)
    raise HTTPException(404)

@app.get("/branding/logo{ext}")
async def logo_serve(ext: str):
    if ext not in _AVATAR_EXTS:
        raise HTTPException(404)
    p = LOGO_DIR / f"logo{ext}"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)

# ============================================================
# downloads
# ============================================================
@app.get("/download/bot/{bid}")
async def download_bot(bid: str, user=Depends(require_user)):
    row = db.fetchone("SELECT name,code,filename FROM bots WHERE id=? AND user_id=?", (bid, user))
    if not row: raise HTTPException(404)
    fname = row["filename"] or "bot.py"
    return Response(content=row["code"] or "",
                    media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})

@app.get("/download/app/{aid}")
async def download_app(aid: str, user=Depends(require_user)):
    row = db.fetchone("SELECT * FROM apps WHERE id=? AND user_id=?", (aid, user))
    if not row: raise HTTPException(404)
    d = _app_dir(aid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if d.exists():
            for f in d.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(d))
        env = _load_env("app", aid)
        if env:
            z.writestr(".env", "\n".join(f"{k}={v}" for k, v in env.items()))
        z.writestr("_meta.json", json.dumps({k: row[k] for k in row}, indent=2, default=str))
    buf.seek(0)
    return Response(content=buf.read(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{row["name"]}.zip"'})

@app.get("/download/backup")
async def download_backup(user=Depends(require_user)):
    payload = {
        "exported_at": datetime.utcnow().isoformat(),
        "user": user,
        "apps": db.fetchall("SELECT * FROM apps WHERE user_id=?", (user,)),
        "bots": db.fetchall("SELECT id,name,filename,status,created_at FROM bots WHERE user_id=?", (user,)),
    }
    for b in payload["bots"]:
        b["code"] = (db.fetchone("SELECT code FROM bots WHERE id=?", (b["id"],)) or {}).get("code", "")
    data = json.dumps(payload, indent=2, default=str)
    return Response(content=data, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="rehan-backup-{datetime.utcnow():%Y%m%d-%H%M}.json"'})

# ============================================================
# settings
# ============================================================
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user=Depends(require_user)):
    fields = []
    for k in EDITABLE_KEYS:
        cur = getattr(settings, k, "") or ""
        is_secret = k in SECRET_KEYS
        fields.append({
            "key": k,
            "value_masked": mask(cur) if is_secret else cur,
            "secret": is_secret,
            "has_value": bool(cur),
        })
    return render("settings.html", request, page="settings",
                  fields=fields,
                  db_mode=settings.DB_MODE,
                  public_url=settings.PUBLIC_URL,
                  admin_user=settings.ADMIN_USER)

@app.post("/settings")
async def settings_save(request: Request, user=Depends(require_user)):
    form = await request.form()
    changed_admin = False
    new_admin_user = None
    new_admin_pass = None
    for k in EDITABLE_KEYS:
        raw = (form.get(k) or "").strip()
        if not raw: continue
        try: db.execute("DELETE FROM app_settings WHERE key=?", (k,))
        except Exception: pass
        db.execute("INSERT INTO app_settings (key, value_enc, updated_at) VALUES (?,?,?)",
                   (k, auth.encrypt(raw), datetime.utcnow().isoformat()))
        try: setattr(settings, k, raw)
        except Exception: pass
        if k == "ADMIN_USER":
            new_admin_user = raw; changed_admin = True
        if k == "ADMIN_PASSWORD":
            new_admin_pass = raw; changed_admin = True

    if changed_admin:
        cur_row = db.fetchone("SELECT id FROM users WHERE username=?", (user,))
        if cur_row:
            db.execute("DELETE FROM users WHERE id=?", (cur_row["id"],))
        new_u = new_admin_user or user
        new_p = new_admin_pass or settings.ADMIN_PASSWORD
        db.ensure_admin(new_u, auth.hash_password(new_p))
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(settings.SESSION_COOKIE)
        return resp
    return RedirectResponse("/settings", 303)

@app.post("/settings/avatar")
async def avatar_upload(request: Request, user=Depends(require_user),
                        file: UploadFile = File(...)):
    if not file or not file.filename:
        return RedirectResponse("/settings", 303)
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in _AVATAR_EXTS:
        raise HTTPException(400, "only png/jpg/jpeg/gif/webp")
    raw = await _read_capped(file, MAX_UPLOAD_IMAGE)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    for e in _AVATAR_EXTS:
        old = AVATAR_DIR / f"{user}{e}"
        if old.exists():
            try: old.unlink()
            except Exception: pass
    (AVATAR_DIR / f"{user}{ext}").write_bytes(raw)
    return RedirectResponse("/settings", 303)

@app.post("/settings/avatar/delete")
async def avatar_delete(user=Depends(require_user)):
    for e in _AVATAR_EXTS:
        p = AVATAR_DIR / f"{user}{e}"
        if p.exists():
            try: p.unlink()
            except Exception: pass
    return RedirectResponse("/settings", 303)

@app.post("/settings/logo")
async def logo_upload(request: Request, user=Depends(require_user),
                      file: UploadFile = File(...)):
    if not file or not file.filename:
        return RedirectResponse("/settings", 303)
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in _AVATAR_EXTS:
        raise HTTPException(400, "only png/jpg/jpeg/gif/webp")
    raw = await _read_capped(file, MAX_UPLOAD_IMAGE)
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    for e in _AVATAR_EXTS:
        old = LOGO_DIR / f"logo{e}"
        if old.exists():
            try: old.unlink()
            except Exception: pass
    (LOGO_DIR / f"logo{ext}").write_bytes(raw)
    return RedirectResponse("/settings", 303)

@app.post("/settings/logo/delete")
async def logo_delete(user=Depends(require_user)):
    for e in _AVATAR_EXTS:
        p = LOGO_DIR / f"logo{e}"
        if p.exists():
            try: p.unlink()
            except Exception: pass
    return RedirectResponse("/settings", 303)


# ============================================================
# live stats — CPU load, ram, uptime
# ============================================================
_BOOT_TS = time.time()
_prev_cpu = {"total": 0, "idle": 0}

def _read_proc_stat():
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = line.split()
        nums = [int(x) for x in parts[1:]]
        total = sum(nums)
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return total, idle
    except Exception:
        return 0, 0

@app.get("/api/stats")
async def api_stats(user=Depends(require_user)):
    # cpu delta based
    total, idle = _read_proc_stat()
    cpu_pct = 0.0
    if total and _prev_cpu["total"]:
        dt = total - _prev_cpu["total"]
        di = idle - _prev_cpu["idle"]
        if dt > 0:
            cpu_pct = max(0.0, min(100.0, 100.0 * (dt - di) / dt))
    _prev_cpu["total"] = total
    _prev_cpu["idle"] = idle
    if not cpu_pct:
        # fallback: loadavg
        try:
            la = float(open("/proc/loadavg").read().split()[0])
            n = os.cpu_count() or 1
            cpu_pct = min(100.0, 100.0 * la / n)
        except Exception:
            cpu_pct = 0.0

    # memory
    mem_total = mem_avail = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) * 1024
    except Exception:
        pass
    mem_pct = 0.0
    if mem_total > 0:
        mem_pct = 100.0 * (mem_total - mem_avail) / mem_total

    return {
        "cpu": round(cpu_pct, 1),
        "ram_pct": round(mem_pct, 1),
        "ram_used_mb": round((mem_total - mem_avail) / 1024 / 1024, 1) if mem_total else 0,
        "ram_total_mb": round(mem_total / 1024 / 1024, 1) if mem_total else 0,
        "cores": os.cpu_count() or 1,
        "uptime": int(time.time() - _BOOT_TS),
        "bots": len(sup.bots),
        "apps": len(appsup.apps),
    }


# ============================================================
# global error capture — debug
# ============================================================
_LAST_ERROR = {"ts": None, "path": None, "traceback": None}

@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    import traceback as _tb
    tb = _tb.format_exc()
    _LAST_ERROR["ts"] = datetime.utcnow().isoformat()
    _LAST_ERROR["path"] = str(request.url.path)
    _LAST_ERROR["traceback"] = tb
    print(f"[UNHANDLED] {request.url.path}\n{tb}", flush=True)
    return JSONResponse(
        {"detail": "internal error", "path": str(request.url.path)},
        status_code=500,
    )

@app.get("/debug/last-error")
async def debug_last_error():
    return _LAST_ERROR

# ============================================================
# health
# ============================================================
@app.get("/healthz")
async def healthz():
    try:
        hint = db.working_url_hint()
    except Exception:
        hint = "?"
    return {"ok": True, "db": settings.DB_MODE, "db_host": hint,
            "bots": len(sup.bots), "apps": len(appsup.apps)}
