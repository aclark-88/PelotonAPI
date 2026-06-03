"""Runtime configuration, sourced from environment variables.

Production runs against Postgres via ``DATABASE_URL``. When it is unset we fall
back to a local SQLite file so the app, the ingestion jobs, and the test suite
all run with zero external infrastructure.
"""
from __future__ import annotations

import os
from pathlib import Path

# Repository root (one level above the app package).
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Load BASE_DIR/.env into the environment (without overriding real env vars).

    Keeps setup friction low: a user can drop SMTP/DB settings in a .env file
    instead of exporting variables. No external dependency required.
    """
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()


def _default_sqlite_url() -> str:
    return f"sqlite:///{BASE_DIR / 'coremont.db'}"


def database_url() -> str:
    """Return the active SQLAlchemy URL.

    Prefers ``DATABASE_URL``. Accepts the bare ``postgres://`` / ``postgresql://``
    forms that some platforms emit and rewrites them to the psycopg2 dialect.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return _default_sqlite_url()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url


def sec_user_agent() -> str:
    return os.environ.get(
        "SEC_USER_AGENT", "Coremont Signal Engine (ops@coremont.example.com)"
    )


def ingest_lookback_days() -> int:
    try:
        return int(os.environ.get("INGEST_LOOKBACK_DAYS", "7"))
    except ValueError:
        return 7


def hubspot_token() -> str | None:
    token = os.environ.get("HUBSPOT_TOKEN", "").strip()
    return token or None


def export_dir() -> Path:
    d = BASE_DIR / "exports"
    d.mkdir(exist_ok=True)
    return d


# --- Daily digest / email -----------------------------------------------------
def smtp_config() -> dict | None:
    """SMTP settings for the daily email digest, or None if not configured.

    Defaults target Gmail (smtp.gmail.com:587, STARTTLS). Use a Gmail
    *app password*, not your normal password.
    """
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not (user and password):
        return None
    return {
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": user,
        "password": password,
        "from_addr": os.environ.get("DIGEST_FROM", user).strip(),
    }


def digest_recipients() -> list[str]:
    raw = os.environ.get("DIGEST_TO", "").strip()
    return [a.strip() for a in raw.split(",") if a.strip()]
