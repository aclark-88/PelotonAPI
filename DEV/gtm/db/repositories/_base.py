"""Shared repository plumbing."""

from __future__ import annotations

from typing import Any

from postgrest.exceptions import APIError
from supabase import Client

from gtm.db.client import get_client

UNIQUE_VIOLATION = "23505"


class BaseRepo:
    def __init__(self, client: Client | None = None) -> None:
        self.client = client or get_client()

    @staticmethod
    def _is_unique_violation(err: APIError) -> bool:
        return getattr(err, "code", None) == UNIQUE_VIOLATION

    @staticmethod
    def _dump(model: Any, **extra: Any) -> dict[str, Any]:
        """Pydantic model -> JSON-safe dict for PostgREST, with overrides."""
        payload = model.model_dump(mode="json", exclude_none=True)
        for k, v in extra.items():
            if v is not None:
                payload[k] = str(v) if hasattr(v, "hex") else v
        return payload
