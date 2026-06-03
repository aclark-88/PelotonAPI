"""Signal detection + sales-ready reason strings.

Turns a manager's filings/vehicles/adviser facts into discrete prospecting
signals (new launch, active raise, momentum, platform/strategy expansion,
structural complexity) and a concise "Why Coremont now?" explanation.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from . import personas
from .scoring import ManagerFacts, ScoreBreakdown

# Signal type constants.
NEW_FUND_LAUNCH = "new_fund_launch"
ACTIVE_CAPITAL_RAISE = "active_capital_raise"
ONGOING_RAISE_MOMENTUM = "ongoing_raise_momentum"
PLATFORM_EXPANSION = "platform_expansion"
STRATEGY_EXPANSION = "strategy_expansion"
STRUCTURAL_COMPLEXITY = "structural_complexity"

LABELS = {
    NEW_FUND_LAUNCH: "New fund launch",
    ACTIVE_CAPITAL_RAISE: "Active capital raise",
    ONGOING_RAISE_MOMENTUM: "Ongoing raise momentum",
    PLATFORM_EXPANSION: "Platform expansion",
    STRATEGY_EXPANSION: "Strategy expansion",
    STRUCTURAL_COMPLEXITY: "Structural complexity",
}


@dataclass
class DetectedSignal:
    signal_type: str
    signal_date: dt.date | None
    strength: float          # 0..1 confidence
    reason: str


def _fmt_usd(amount: float | None) -> str | None:
    if not amount:
        return None
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.0f}M"
    return f"${amount:,.0f}"


def detect_signals(
    facts: ManagerFacts,
    *,
    display_name: str,
    tags: list[str],
    newest_vehicle_name: str | None = None,
    today: dt.date | None = None,
) -> list[DetectedSignal]:
    today = today or dt.date.today()
    out: list[DetectedSignal] = []

    new_filings = [f for f in facts.filings if not f.is_amendment]
    amendments = [f for f in facts.filings if f.is_amendment]

    def age(d: dt.date | None) -> int | None:
        return (today - d).days if d else None

    fresh_new = [f for f in new_filings if (age(f.filing_date) or 999) <= 30]
    vehicle_phrase = f'"{newest_vehicle_name}" ' if newest_vehicle_name else ""

    # New fund launch — a recent new (non-amendment) Form D.
    if fresh_new:
        days = min(age(f.filing_date) for f in fresh_new if f.filing_date is not None)
        out.append(
            DetectedSignal(
                NEW_FUND_LAUNCH,
                min((f.filing_date for f in fresh_new if f.filing_date), default=today),
                0.9,
                f"New {vehicle_phrase}vehicle filed Form D within {days} days.",
            )
        )

    # Active capital raise — non-zero amount sold or a meaningful target.
    raising = [f for f in facts.filings if (f.amount_sold or 0) > 0 or (f.offering_amount or 0) > 0]
    if raising:
        best = max(raising, key=lambda f: (f.amount_sold or 0, f.offering_amount or 0))
        sold = _fmt_usd(best.amount_sold)
        target = _fmt_usd(best.offering_amount)
        bits = []
        if sold:
            bits.append(f"{sold} sold")
        if target:
            bits.append(f"{target} target")
        out.append(
            DetectedSignal(
                ACTIVE_CAPITAL_RAISE,
                best.first_sale_date or best.filing_date,
                0.8 if best.amount_sold else 0.6,
                "Active raise: " + (", ".join(bits) if bits else "non-zero offering") + ".",
            )
        )

    # Ongoing raise momentum — an amendment to a prior filing.
    if amendments:
        recent = min(amendments, key=lambda f: age(f.filing_date) or 999)
        out.append(
            DetectedSignal(
                ONGOING_RAISE_MOMENTUM,
                recent.filing_date,
                0.6,
                "Form D/A amendment indicates the vehicle remains live and raising.",
            )
        )

    # Platform expansion — adviser footprint or multiple vehicles.
    if facts.adviser_fund_count >= 2 or facts.vehicle_count >= 2:
        n = max(facts.adviser_fund_count, facts.vehicle_count)
        out.append(
            DetectedSignal(
                PLATFORM_EXPANSION,
                today,
                0.7 if facts.adviser_known else 0.5,
                f"Adviser/platform shows {n} related private funds — institutionalizing operations.",
            )
        )

    # Strategy expansion — strong ICP strategy overlap present.
    if tags:
        out.append(
            DetectedSignal(
                STRATEGY_EXPANSION,
                today,
                0.7,
                f"Strategy overlap with Clarion ICP: {', '.join(tags[:5])}.",
            )
        )

    # Structural complexity — master/feeder/offshore patterns.
    if facts.has_master or facts.has_feeder or facts.has_offshore:
        parts = []
        if facts.has_master:
            parts.append("master")
        if facts.has_feeder:
            parts.append("feeder")
        if facts.has_offshore:
            parts.append("offshore")
        out.append(
            DetectedSignal(
                STRUCTURAL_COMPLEXITY,
                today,
                0.65,
                f"{'/'.join(parts)} structure raises strain on risk, treasury, and P&L workflows.",
            )
        )

    return out


def summary_reason(
    display_name: str,
    signals: list[DetectedSignal],
    breakdown: ScoreBreakdown,
) -> str:
    """A single concise, sales-ready reason string for the manager."""
    if not signals:
        return f"{display_name}: no fresh filing signals."
    # Order by strength, take the headline drivers.
    ordered = sorted(signals, key=lambda s: -s.strength)
    drivers = [LABELS[s.signal_type].lower() for s in ordered[:3]]
    tag_str = f" ({', '.join(breakdown.tags[:3])})" if breakdown.tags else ""
    return (
        f"{display_name}: " + "; ".join(s.reason for s in ordered[:3])
        + f" Drivers: {', '.join(drivers)}{tag_str}. Tier {breakdown.tier}, score {breakdown.total:.0f}."
    )


def why_coremont_now(signals: list[DetectedSignal]) -> str:
    """Plain-English Clarion pain framing for the manager detail page."""
    if not signals:
        return "No active filing signals; monitor for new Form D activity."
    pains = []
    seen = set()
    for s in sorted(signals, key=lambda s: -s.strength):
        pain = personas.PAIN_BY_SIGNAL.get(s.signal_type)
        if pain and pain not in seen:
            pains.append(pain)
            seen.add(pain)
    lead = "Recent filings point to " + "; ".join(pains[:3]) + "."
    return lead + " Clarion delivers real-time, cross-book risk and P&L on a single platform exactly when this complexity lands."
