import sqlite3, uuid, re, time
from datetime import datetime
from config import settings

_IS_PG = settings.DB_MODE == "supabase"

if _IS_PG:
    import psycopg
    from psycopg.rows import dict_row

SCHEMA = [
"""CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, created_at TEXT)""",
"""CREATE TABLE IF NOT EXISTS apps (
  id TEXT PRIMARY KEY, user_id TEXT, name TEXT, type TEXT, repo TEXT,
  service_id TEXT, url TEXT, status TEXT, created_at TEXT,
  entry TEXT, run_cmd TEXT, static_root TEXT,
  requirements TEXT, deps_hash TEXT)""",
"""CREATE TABLE IF NOT EXISTS bots (
  id TEXT PRIMARY KEY, user_id TEXT, name TEXT, code TEXT,
  token_enc TEXT, filename TEXT, webhook_secret TEXT, status TEXT, created_at TEXT,
  requirements TEXT, deps_hash TEXT)""",
"""CREATE TABLE IF NOT EXISTS env_vars (
  id TEXT PRIMARY KEY, owner_type TEXT, owner_id TEXT, key TEXT, value_enc TEXT)""",
"""CREATE TABLE IF NOT EXISTS logs (
  id TEXT PRIMARY KEY, owner_type TEXT, owner_id TEXT, level TEXT, message TEXT, ts TEXT)""",
"""CREATE TABLE IF NOT EXISTS settings (
  id TEXT PRIMARY KEY, user_id TEXT, render_token_enc TEXT, render_owner_id TEXT)""",
"""CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY, value_enc TEXT, updated_at TEXT)""",
]


# ============================================================
# connection candidate builder — auto-detect region for pooler
# ============================================================
_CANDIDATES = None
_WORKING_URL = None
_WORKING_TS = 0

_REGIONS = [
    "ap-south-1", "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
    "ap-northeast-2", "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3",
    "ca-central-1", "sa-east-1",
]
_POOL_PREFIXES = ["aws-0", "aws-1"]


def _build_candidates():
    global _CANDIDATES
    if _CANDIDATES is not None:
        return _CANDIDATES
    url = settings.DATABASE_URL or ""
    out = []
    seen = set()

    def push(u):
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    # 1. whatever user set
    push(url)

    # 2. parse and generate alternatives
    m = re.match(r"postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)", url)
    if m:
        user, pwd, host, port, rest = m.groups()
        ref = user.split(".")[-1] if "." in user else ""
        if ref and ref.startswith("postgres") is False:
            pass
        if not ref or ref == "postgres":
            # try extracting from host db.<ref>.supabase.co
            m2 = re.match(r"db\.([a-z0-9]+)\.supabase\.co", host)
            if m2:
                ref = m2.group(1)
        if ref and ref != "postgres":
            # direct connection first (fastest, IPv4 may work)
            push(f"postgresql://postgres:{pwd}@db.{ref}.supabase.co:5432/postgres?sslmode=require")
            # then pooler regions
            for region in _REGIONS:
                for pref in _POOL_PREFIXES:
                    push(
                        f"postgresql://postgres.{ref}:{pwd}@"
                        f"{pref}-{region}.pooler.supabase.com:6543/postgres?sslmode=require"
                    )

    _CANDIDATES = out
    return out


def _pg_connect():
    """try cached working URL first, else walk candidates"""
    global _WORKING_URL, _WORKING_TS

    if _WORKING_URL:
        try:
            return psycopg.connect(_WORKING_URL, row_factory=dict_row, connect_timeout=8)
        except Exception:
            _WORKING_URL = None

    last_exc = None
    for url in _build_candidates():
        try:
            c = psycopg.connect(url, row_factory=dict_row, connect_timeout=8)
            _WORKING_URL = url
            _WORKING_TS = time.time()
            return c
        except Exception as e:
            last_exc = e
            continue
    raise last_exc or RuntimeError("no DATABASE_URL candidate worked")


def _conn():
    if _IS_PG:
        return _pg_connect()
    c = sqlite3.connect(settings.SQLITE_PATH, check_same_thread=False, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def _q(sql: str) -> str:
    return sql.replace("?", "%s") if _IS_PG else sql


def init():
    conn = _conn(); cur = conn.cursor()
    for stmt in SCHEMA:
        try:
            cur.execute(stmt)
        except Exception as e:
            print(f"[db.init] {e}")
    conn.commit()
    try:
        conn.close()
    except Exception:
        pass


def execute(sql, params=()):
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute(_q(sql), params)
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass


def fetchone(sql, params=()):
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute(_q(sql), params)
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try: conn.close()
        except Exception: pass


def fetchall(sql, params=()):
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute(_q(sql), params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        try: conn.close()
        except Exception: pass


def ensure_admin(username, password_hash):
    if fetchone("SELECT id FROM users WHERE username=?", (username,)):
        return
    execute(
        "INSERT INTO users (id,username,password_hash,created_at) VALUES (?,?,?,?)",
        (uuid.uuid4().hex[:12], username, password_hash,
         datetime.utcnow().isoformat()),
    )


# expose for health check
def working_url_hint() -> str:
    if not _WORKING_URL:
        return "unknown"
    m = re.match(r"postgresql://[^@]+@([^/]+)", _WORKING_URL)
    return m.group(1) if m else "unknown"

def migrate_runtime_columns():
    """Add requirements/deps_hash columns if missing (sqlite + pg safe)."""
    for table in ("bots", "apps"):
        for col in ("requirements", "deps_hash"):
            try:
                db_check = fetchone(f"SELECT {col} FROM {table} LIMIT 1")
            except Exception:
                try:
                    execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                except Exception:
                    pass
