"""Source-run and raw-payload provenance models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from gtm.models.common import GTMModel, RunStatus


class SourceRun(GTMModel):
    """source_runs has audit columns but no source_run_id (it IS the run)."""

    id: UUID
    skill_name: str
    started_at: datetime
    ended_at: datetime | None = None
    status: RunStatus = RunStatus.running
    records_processed: int = 0
    records_inserted: int = 0
    records_updated: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    created_by: str = "system"
    deleted_at: datetime | None = None


class RawPayload(GTMModel):
    id: UUID
    source: str
    source_run_id: UUID | None = None
    request: dict[str, Any] | None = None
    response: dict[str, Any] | list[Any]
    fetched_at: datetime
    payload_hash: str
    created_at: datetime
    updated_at: datetime
    created_by: str = "system"
    deleted_at: datetime | None = None
