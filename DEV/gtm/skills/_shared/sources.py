"""External data sources: typed protocols + the availability seam.

Every source the skills consume is duck-typed against a Protocol so tests can
inject fakes and so a source can be backed by:
  1. a direct client (EDGAR via edgartools, Apollo/HeyReach/HubSpot via REST,
     Tavily, OpenAI, Anthropic) — the normal case;
  2. prefetched data injected by an orchestrating Claude session holding an
     MCP connection (kept as an option; Apollo got a REST key so it is tier 1);
  3. nothing — ctx.sources.require() raises SourceUnavailable, which a skill
     catches to degrade to status=partial with a clear error instead of
     crashing a scheduled run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class SourceUnavailable(Exception):
    """The requested source is not configured in this run."""


# ── Normalized EDGAR records (what skills consume; fakes return these) ───────

class FormDRecord(BaseModel):
    """One Form D filing, normalized."""

    accession: str
    cik: str
    issuer_name: str
    filed_at: datetime
    is_amendment: bool = False
    industry_group: str | None = None          # e.g. "Pooled Investment Fund"
    fund_type: str | None = None               # e.g. "Hedge Fund"
    total_offering_usd: float | None = None    # None = indefinite
    total_sold_usd: float | None = None
    investor_count: int | None = None
    related_persons: list[dict[str, Any]] = Field(default_factory=list)
    state: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class AdvProfile(BaseModel):
    """Firm-level Form ADV data (SEC monthly FOIA roster / IAPD — not EDGAR)."""

    crd: str
    firm_name: str
    regulatory_aum_usd: float | None = None    # in USD millions
    aum_as_of: str | None = None               # ISO date string when known
    pct_private_fund: float | None = None      # share of RAUM in private funds
    strategies: list[str] = Field(default_factory=list)
    prime_brokers: list[str] = Field(default_factory=list)
    custodians: list[str] = Field(default_factory=list)
    administrator: str | None = None
    website: str | None = None                 # bare domain, social hosts filtered
    headquarters_city: str | None = None
    headquarters_country: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ThirteenFSnapshot(BaseModel):
    """One quarter of 13F holdings, reduced to intensity inputs."""

    cik: str
    period: str                                # e.g. "2026-03-31"
    position_count: int
    total_value_usd: float
    option_position_count: int = 0             # PUT/CALL rows
    top10_concentration: float | None = None   # share of value in top 10
    positions: list[str] = Field(default_factory=list)  # issuer keys, for turnover


@runtime_checkable
class EdgarSourceP(Protocol):
    def recent_form_d(self, lookback_days: int, max_filings: int = 200) -> list[FormDRecord]: ...
    def form_d_history_count(self, cik: str) -> int: ...
    def adv_firm_profile(self, crd: str | None = None, name: str | None = None, cik: str | None = None) -> AdvProfile | None: ...
    def thirteen_f_quarters(self, cik: str, quarters: int = 4) -> list[ThirteenFSnapshot]: ...


class SourceBundle(BaseModel):
    """Everything external a skill may touch. None = not configured."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    edgar: Any | None = None
    apollo: Any | None = None
    hubspot: Any | None = None
    heyreach: Any | None = None
    web: Any | None = None
    embedder: Any | None = None
    llm: Any | None = None
    slack: Any | None = None

    def require(self, name: str) -> Any:
        source = getattr(self, name, None)
        if source is None:
            raise SourceUnavailable(
                f"source '{name}' is not configured for this run — "
                f"check credentials / SourceBundle construction"
            )
        return source
