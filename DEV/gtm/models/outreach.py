"""Campaign, draft, attempt, and reply models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from gtm.models.common import (
    AuditedRow,
    Channel,
    GTMModel,
    OutreachStatus,
    ReplyIntent,
    ReplySentiment,
    parse_vector,
)


class CampaignIn(GTMModel):
    name: str
    signal_type_key: str | None = None
    channel: Channel = Channel.email
    apollo_sequence_id: str | None = None
    heyreach_campaign_id: str | None = None
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class Campaign(AuditedRow, CampaignIn):
    pass


class DraftIn(GTMModel):
    person_id: UUID
    body: str
    signal_id: UUID | None = None
    campaign_id: UUID | None = None
    channel: Channel = Channel.email
    variant_label: str | None = None
    subject: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    embedding: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _parse_embedding = field_validator("embedding", mode="before")(parse_vector)


class Draft(AuditedRow, DraftIn):
    approved_by: str | None = None
    approved_at: datetime | None = None
    sent_attempt_id: UUID | None = None


class OutreachAttemptIn(GTMModel):
    person_id: UUID
    campaign_id: UUID
    signal_id: UUID | None = None
    channel: Channel = Channel.email
    step_number: int = 1
    sent_at: datetime | None = None
    status: OutreachStatus = OutreachStatus.queued
    external_id: str | None = None
    draft_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutreachAttempt(AuditedRow, OutreachAttemptIn):
    pass


class ReplyIn(GTMModel):
    outreach_attempt_id: UUID
    body: str | None = None
    received_at: datetime | None = None
    sentiment: ReplySentiment | None = None
    intent: ReplyIntent | None = None
    embedding: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _parse_embedding = field_validator("embedding", mode="before")(parse_vector)


class Reply(AuditedRow, ReplyIn):
    pass
