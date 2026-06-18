"""Fund models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from gtm.models.common import AuditedRow, GTMModel, parse_vector


class FundIn(GTMModel):
    """Input payload for creating/updating a fund."""

    legal_name: str
    common_name: str | None = None
    crd: str | None = None
    lei: str | None = None
    cik: str | None = None
    primary_domain: str | None = None

    aum_usd_millions: float | None = None
    aum_as_of: date | None = None
    strategies: list[str] = Field(default_factory=list)
    is_emerging_manager: bool | None = None
    parent_fund_id: UUID | None = None

    headquarters_city: str | None = None
    headquarters_country: str | None = None
    inception_date: date | None = None
    prime_brokers: list[str] = Field(default_factory=list)
    administrator: str | None = None
    custodians: list[str] = Field(default_factory=list)
    known_incumbent_pms: list[str] = Field(default_factory=list)

    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Fund(AuditedRow, FundIn):
    """A fund row as stored. aum_band is DB-generated; fit_score/tier are caches."""

    aum_band: str = "unknown"
    fit_score: int | None = Field(default=None, ge=0, le=100)
    fit_score_updated_at: datetime | None = None
    tier: int | None = Field(default=None, ge=1, le=4)


class FundSummary(AuditedRow):
    fund_id: UUID
    summary_text: str
    embedding: list[float] | None = None
    _parse_embedding = field_validator("embedding", mode="before")(parse_vector)
    embedding_model: str | None = None
    generated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
