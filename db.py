import sqlite3, uuid
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
  entry TEXT, run_cmd TEXT, static_root TEXT)""",
"""CREATE TABLE IF NOT EXISTS bots (
  id TEXT PRIMARY KEY, user_id TEXT, name TEXT, token_enc TEXT, code TEXT,
  filename TEXT, webhook_secret TEXT, status TEXT, created_at TEXT)""",
"""CREATE TABLE IF NOT EXISTS env_vars (
  id TEXT PRIMARY KEY, owner_type TEXT, owner_id TEXT, key TEXT, value_enc TEXT)""",
"""CREATE TABLE IF NOT EXISTS logs (
  id TEXT PRIMARY KEY, owner_type TEXT, owner_id TEXT, level TEXT, message TEXT, ts TEXT)""",
"""CREATE TABLE IF NOT EXISTS settings (
  id TEXT PRIMARY KEY, user_id TEXT, render_token_enc TEXT, render_owner_id TEXT)""",
"""CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY, value_enc TEXT, updated_at TEXT)""",
]

def _conn():
    if _IS_PG:
        return psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)
    c = sqlite3.connect(settings.SQLITE_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def _q(sql: str) -> str:
    return sql.replace("?", "%s") if _IS_PG else sql

def _cur(conn):
    return conn.cursor()

def init():
    conn = _conn(); cur = conn.cursor()
    for stmt in SCHEMA:
        cur.execute(stmt)
    conn.commit(); conn.close()

def execute(sql, params=()):
    conn = _conn(); cur = conn.cursor()
    cur.execute(_q(sql), params)
    conn.commit(); conn.close()

def fetchone(sql, params=()):
    conn = _conn(); cur = _cur(conn)
    cur.execute(_q(sql), params)
    row = cur.fetchone(); conn.close()
    return dict(row) if row else None

def fetchall(sql, params=()):
    conn = _conn(); cur = _cur(conn)
    cur.execute(_q(sql), params)
    rows = cur.fetchall(); conn.close()
    return [dict(r) for r in rows]

def ensure_admin(username, password_hash):
    if fetchone("SELECT id FROM users WHERE username=?", (username,)):
        return
    execute("INSERT INTO users (id,username,password_hash,created_at) VALUES (?,?,?,?)",
            (uuid.uuid4().hex[:12], username, password_hash, datetime.utcnow().isoformat()))
