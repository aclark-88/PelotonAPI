"""Person and employment-history models."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from pydantic import Field

from gtm.models.common import AuditedRow, GTMModel, RoleFunction, Seniority


class PersonIn(GTMModel):
    full_name: str
    email: str | None = None
    linkedin_url: str | None = None
    apollo_id: str | None = None
    current_fund_id: UUID | None = None
    current_role: str | None = None
    current_role_seniority: Seniority = Seniority.unknown
    current_role_function: RoleFunction = RoleFunction.unknown
    role_started_at: date | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Person(AuditedRow, PersonIn):
    is_buying_committee_member: bool = False  # trigger-maintained, read-only


class EmploymentHistory(AuditedRow):
    person_id: UUID
    fund_id: UUID
    role: str | None = None
    function: RoleFunction = RoleFunction.unknown
    seniority: Seniority = Seniority.unknown
    started_at: date | None = None
    ended_at: date | None = None
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
