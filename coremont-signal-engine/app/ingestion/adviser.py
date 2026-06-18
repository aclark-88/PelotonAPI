"""Job 3: Adviser (IAPD) enrichment.

In production this layer maps a manager to its Investment Adviser Public
Disclosure record (IARD/CRD identity, disclosed business profile, and
private-fund footprint from Form ADV) to distinguish a real hedge-fund platform
from a one-off EDGAR issuer name.

Live IAPD access is gated/credentialed in many environments, so v1 ships a
deterministic enrichment that derives adviser context from what we already hold
(vehicle count, structure, names) and is safely overridden by a real connector
or a seeded ``research_notes`` entry. The interface is what matters: callers get
an ``AdviserContext`` regardless of source.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Manager


@dataclass
class AdviserContext:
    adviser_known: bool
    adviser_fund_count: int
    business_summary: str | None = None
    source_url: str | None = None


def enrich_manager(session: Session, manager: Manager) -> AdviserContext:
    """Derive adviser context for a manager.

    A manager mapped to multiple vehicles, or holding an IARD id, is treated as a
    recognised adviser platform; otherwise it is a single-issuer candidate.
    """
    vehicle_count = len(manager.vehicles)
    has_iard = bool(manager.sec_iard_id)
    # Multiple vehicles under one normalized platform is a strong adviser signal.
    adviser_known = has_iard or vehicle_count >= 2
    fund_count = max(vehicle_count, 1)

    summary = None
    if adviser_known:
        summary = (
            f"Adviser platform mapped to {vehicle_count} private fund "
            f"vehicle{'s' if vehicle_count != 1 else ''}"
            + (f" (IARD {manager.sec_iard_id})" if has_iard else "")
            + "."
        )
    source = (
        f"https://adviserinfo.sec.gov/firm/summary/{manager.sec_iard_id}"
        if has_iard
        else None
    )
    return AdviserContext(
        adviser_known=adviser_known,
        adviser_fund_count=fund_count,
        business_summary=summary,
        source_url=source,
    )
