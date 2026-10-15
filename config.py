import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
AVATAR_DIR = UPLOAD_DIR / "avatars"
LOGO_DIR = UPLOAD_DIR / "branding"

# --- local .env loader (never commit .env) ---
_ENV_PATH = BASE_DIR / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        _k = _k.strip()
        _v = _v.strip().strip('"').strip("'")
        if _k and _k not in os.environ:
            os.environ[_k] = _v

def _env(k: str, default: str = "") -> str:
    return os.getenv(k, default)


class Settings:
    APP_NAME = "Rehan Web Hosting"

    # --- auth / session ---
    SECRET_KEY = _env("SECRET_KEY", "")
    ADMIN_USER = _env("ADMIN_USER", "Rehan")
    ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "")

    # --- database ---
    DB_MODE = _env("DB_MODE", "sqlite").lower()
    SQLITE_PATH = _env("SQLITE_PATH", str(BASE_DIR / "panel.db"))
    DATABASE_URL = _env("DATABASE_URL", "")

    # --- supabase (reference) ---
    SUPABASE_URL = _env("SUPABASE_URL", "")
    SUPABASE_PUBLISHABLE_KEY = _env("SUPABASE_PUBLISHABLE_KEY", "")
    SUPABASE_SECRET_KEY = _env("SUPABASE_SECRET_KEY", "")

    # --- panel ---
    PUBLIC_URL = _env("PUBLIC_URL", "").rstrip("/")
    SESSION_COOKIE = "rwh_session"
    SESSION_MAX_AGE = 60 * 60 * 24 * 7
    PORT = int(_env("PORT", "8000"))

settings = Settings()

# editable keys — settings page theke change kora jabe
EDITABLE_KEYS = [
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SECRET_KEY",
    "DATABASE_URL",
    "ADMIN_USER",
    "ADMIN_PASSWORD",
    "PUBLIC_URL",
]

SECRET_KEYS = {
    "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SECRET_KEY",
    "DATABASE_URL", "ADMIN_PASSWORD",
}
