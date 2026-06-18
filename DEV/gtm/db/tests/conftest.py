"""Integration-test fixtures.

These tests run against a real Supabase instance (local stack or the linked
remote project). They skip cleanly when SUPABASE_URL /
SUPABASE_SERVICE_ROLE_KEY are not configured (env or .env).

Test data uses a per-session unique suffix and is soft-deleted on teardown —
the system never hard-deletes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


@pytest.fixture(scope="session")
def db():
    try:
        from gtm.db.client import get_client, get_settings

        settings = get_settings()
        configured = bool(settings.supabase_url and settings.supabase_service_role_key)
    except Exception as exc:  # pydantic ValidationError on missing env
        pytest.skip(f"Supabase credentials not configured: {exc}")
    if not configured:
        pytest.skip("Supabase credentials not configured (SUPABASE_SERVICE_ROLE_KEY is empty)")
    return get_client()


@pytest.fixture(scope="session")
def run_suffix() -> str:
    """Unique-per-session marker so unique indexes never collide across runs."""
    return uuid.uuid4().hex[:12]


@pytest.fixture(scope="session")
def cleanup(db):
    """Collects (table, id) pairs and soft-deletes them after the session."""
    created: list[tuple[str, str]] = []
    yield created
    now = datetime.now(timezone.utc).isoformat()
    # Reverse order: children first, so nothing ever blocks on RESTRICT FKs
    # (soft delete is an update, but keep the discipline anyway).
    for table, row_id in reversed(created):
        try:
            db.table(table).update({"deleted_at": now}).eq("id", row_id).execute()
        except Exception:
            pass  # best-effort teardown; rows are tagged with the run suffix
