"""Signal, signal-type, and scoring models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from gtm.models.common import AuditedRow, GTMModel, Urgency


class SignalType(GTMModel):
    key: str
    display_name: str
    default_urgency: Urgency = Urgency.this_month
    default_score_weight: float = 1.0
    active: bool = True
    description: str | None = None


class SignalIn(GTMModel):
    """A normalized observed event.

    source_record_id is the natural key in the source system; together with
    source and signal_type it forms the dedupe key. Manual signals should
    pass a generated uuid string.
    """

    signal_type: str
    source: str
    source_record_id: str
    observed_at: datetime
    payload: dict[str, Any]
    fund_id: UUID | None = None
    person_id: UUID | None = None
    urgency: Urgency = Urgency.this_month
    urgency_score: int | None = Field(default=None, ge=0, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Signal(AuditedRow, SignalIn):
    ingested_at: datetime
    dedupe_key: str
    superseded_by: UUID | None = None


class ScoringRunIn(GTMModel):
    entity_type: str  # fund | signal | person (DB check constraint)
    entity_id: UUID
    model_version: str
    score: float | None = None
    reasoning: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


class ScoringRun(AuditedRow, ScoringRunIn):
    run_at: datetime
