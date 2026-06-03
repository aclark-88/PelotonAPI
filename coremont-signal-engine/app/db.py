"""Database engine / session plumbing shared by the web app and jobs."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from . import config

_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        url = config.database_url()
        connect_args = {}
        if url.startswith("sqlite"):
            # Allow use across threads (uvicorn workers) and keep FK semantics.
            connect_args = {"check_same_thread": False}
        _engine = create_engine(url, future=True, connect_args=connect_args)
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session for jobs and scripts."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency — one session per request."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """Create all tables. Idempotent."""
    from .models import Base  # local import to avoid cycles

    Base.metadata.create_all(bind=get_engine())


def reset_state_for_tests() -> None:
    """Drop cached engine/session so a new DATABASE_URL takes effect (tests)."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
