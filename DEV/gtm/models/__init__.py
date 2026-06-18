"""Pydantic models mirroring the Supabase schema. One import point."""

from gtm.models.common import (
    AuditedRow,
    Channel,
    GTMModel,
    OutreachStatus,
    ReplyIntent,
    ReplySentiment,
    RoleFunction,
    RunStatus,
    Seniority,
    SyncStatus,
    Urgency,
)
from gtm.models.funds import Fund, FundIn, FundSummary
from gtm.models.outreach import (
    Campaign,
    CampaignIn,
    Draft,
    DraftIn,
    OutreachAttempt,
    OutreachAttemptIn,
    Reply,
    ReplyIn,
)
from gtm.models.people import EmploymentHistory, Person, PersonIn
from gtm.models.runs import RawPayload, SourceRun
from gtm.models.signals import Signal, SignalIn, SignalType, ScoringRun, ScoringRunIn
from gtm.models.sync import HubspotSync

__all__ = [
    "AuditedRow", "Channel", "GTMModel", "OutreachStatus", "ReplyIntent",
    "ReplySentiment", "RoleFunction", "RunStatus", "Seniority", "SyncStatus",
    "Urgency",
    "Fund", "FundIn", "FundSummary",
    "Campaign", "CampaignIn", "Draft", "DraftIn",
    "OutreachAttempt", "OutreachAttemptIn", "Reply", "ReplyIn",
    "EmploymentHistory", "Person", "PersonIn",
    "RawPayload", "SourceRun",
    "Signal", "SignalIn", "SignalType", "ScoringRun", "ScoringRunIn",
    "HubspotSync",
]
