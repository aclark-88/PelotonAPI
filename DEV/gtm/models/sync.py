"""HubSpot reconciliation models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from gtm.models.common import AuditedRow, SyncStatus


class HubspotSync(AuditedRow):
    entity_type: str  # fund | person (DB check constraint)
    local_id: UUID
    hubspot_object_type: str  # company | contact | deal
    hubspot_id: str | None = None
    last_synced_at: datetime | None = None
    sync_status: SyncStatus = SyncStatus.pending
    sync_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
