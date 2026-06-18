"""Shared enums and base model classes mirroring the Postgres schema."""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


def parse_vector(value: Any) -> Any:
    """PostgREST returns pgvector columns as '[0.1,0.2,...]' strings; models
    declare list[float]. Use as a mode='before' field validator."""
    if isinstance(value, str):
        return json.loads(value)
    return value


class Seniority(str, Enum):
    c_suite = "c_suite"
    head = "head"
    vp = "vp"
    ic = "ic"
    unknown = "unknown"


class RoleFunction(str, Enum):
    tech = "tech"
    risk = "risk"
    ops = "ops"
    finance = "finance"
    trading = "trading"
    investment = "investment"
    executive = "executive"
    unknown = "unknown"


class Urgency(str, Enum):
    immediate = "immediate"
    this_week = "this_week"
    this_month = "this_month"
    archive = "archive"


class OutreachStatus(str, Enum):
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    opened = "opened"
    replied = "replied"
    bounced = "bounced"
    failed = "failed"
    unsubscribed = "unsubscribed"


class ReplySentiment(str, Enum):
    positive = "positive"
    neutral = "neutral"
    negative = "negative"
    autoresponder = "autoresponder"
    ooo = "ooo"


class ReplyIntent(str, Enum):
    meeting_request = "meeting_request"
    objection = "objection"
    unsubscribe = "unsubscribe"
    referral = "referral"
    nurture = "nurture"


class SyncStatus(str, Enum):
    pending = "pending"
    synced = "synced"
    failed = "failed"


class RunStatus(str, Enum):
    running = "running"
    success = "success"
    failed = "failed"
    partial = "partial"


class Channel(str, Enum):
    email = "email"
    linkedin = "linkedin"
    multi = "multi"  # campaigns only


class GTMModel(BaseModel):
    """Base for all models: ORM-friendly, tolerant of extra DB columns."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class AuditedRow(GTMModel):
    """Columns present on every table (output models only)."""

    id: UUID
    created_at: datetime
    updated_at: datetime
    created_by: str = "system"
    source_run_id: UUID | None = None
    deleted_at: datetime | None = None
