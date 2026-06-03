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
