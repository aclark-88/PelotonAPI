"""Transparent 100-point rules engine for Clarion-fit scoring.

Four buckets, capped independently, summed to 0-100 and mapped to four tiers.
Every point is explainable — `ScoreBreakdown.lines` records each rule that fired
so a rep (and a tuning analyst chasing win/loss feedback) sees exactly why a
manager scored the way it did.

    Event strength  30   new Form D <=30d (+18), first sale <=45d (+8), D/A (+4),
                         established multi-fund platform (+8 for 3+, +4 for 2)
    Strategy fit    30   weighted keyword overlap (taxonomy), minus low-fit terms
    Complexity      25   multi-vehicle (+8), feeder/master (+6), offshore (+6),
                         adviser footprint (+5)
    Reachability    15   resolved identity (+6), known buyers (+5), outreach path (+4)
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import taxonomy

# Bucket caps.
CAP_EVENT = 30
CAP_STRATEGY = 30
CAP_COMPLEXITY = 25
CAP_REACHABILITY = 15

# Tier thresholds.
TIER_1_MIN = 75
TIER_2_MIN = 55
TIER_3_MIN = 35


@dataclass
class FilingFacts:
    """Minimal filing view the scorer needs (decoupled from the ORM)."""

    filing_date: dt.date | None
    first_sale_date: dt.date | None
    is_amendment: bool
    amount_sold: float | None = None
    offering_amount: float | None = None


@dataclass
class ManagerFacts:
    """Everything the scorer needs about a manager, assembled by the signal job."""

    text: str  # concatenated manager + vehicle names + adviser business text
    filings: list[FilingFacts] = field(default_factory=list)
    vehicle_count: int = 0
    has_master: bool = False
    has_feeder: bool = False
    has_offshore: bool = False
    adviser_known: bool = False         # mapped to an IAPD/adviser record
    adviser_fund_count: int = 0          # private funds disclosed by the adviser
    identity_resolved: bool = True       # normalized to a single platform
    has_known_buyers: bool = False       # at least one mapped contact/persona
    has_outreach_path: bool = False      # website or HQ location known


@dataclass
class ScoreBreakdown:
    event_strength: float = 0.0
    strategy_fit: float = 0.0
    complexity: float = 0.0
    reachability: float = 0.0
    tags: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(
            self.event_strength + self.strategy_fit + self.complexity + self.reachability,
            1,
        )

    @property
    def tier(self) -> int:
        return tier_for_score(self.total)


def tier_for_score(score: float) -> int:
    if score >= TIER_1_MIN:
        return 1
    if score >= TIER_2_MIN:
        return 2
    if score >= TIER_3_MIN:
        return 3
    return 4


def _days_since(d: dt.date | None, today: dt.date) -> int | None:
    if d is None:
        return None
    return (today - d).days


def _score_event_strength(facts: ManagerFacts, today: dt.date, b: ScoreBreakdown) -> None:
    score = 0.0
    new_filings = [f for f in facts.filings if not f.is_amendment]
    amendments = [f for f in facts.filings if f.is_amendment]

    # Freshest *new* Form D within 30 days.
    new_ages = [a for a in (_days_since(f.filing_date, today) for f in new_filings) if a is not None]
    if new_ages and min(new_ages) <= 30 and min(new_ages) >= 0:
        score += 18
        b.lines.append(f"+18 new Form D filed {min(new_ages)}d ago (≤30d)")

    # Any first sale within 45 days → active raise in motion.
    sale_ages = [
        a
        for a in (_days_since(f.first_sale_date, today) for f in facts.filings)
        if a is not None
    ]
    if sale_ages and min(sale_ages) <= 45 and min(sale_ages) >= 0:
        score += 8
        b.lines.append(f"+8 first sale {min(sale_ages)}d ago (≤45d)")

    # Amendment activity → vehicle still live / raising.
    amend_ages = [
        a for a in (_days_since(f.filing_date, today) for f in amendments) if a is not None
    ]
    if amend_ages and min(amend_ages) <= 120 and min(amend_ages) >= 0:
        score += 4
        b.lines.append("+4 recent Form D/A amendment activity")

    # Established multi-fund platform = sustained capital-formation activity. A
    # mature platform is a strong, time-independent buying signal that raw
    # filing-freshness structurally under-credits (the best buyers are rarely
    # brand-new single vehicles), so reward platform breadth here too.
    if facts.adviser_fund_count >= 3 or facts.vehicle_count >= 3:
        score += 8
        b.lines.append("+8 established multi-fund platform (3+ related funds)")
    elif facts.adviser_fund_count >= 2 or facts.vehicle_count >= 2:
        score += 4
        b.lines.append("+4 multi-fund platform (2 related funds)")

    b.event_strength = min(score, CAP_EVENT)


def _score_strategy_fit(facts: ManagerFacts, b: ScoreBreakdown) -> None:
    match = taxonomy.match_text(facts.text)
    raw = match.positive_weight + match.negative_weight  # negatives are already <0
    score = max(0.0, min(float(raw), CAP_STRATEGY))
    b.strategy_fit = score
    b.tags = match.tags
    if match.positive_weight:
        top = sorted(
            (t for t, w in match.matched.items() if w > 0),
            key=lambda t: -match.matched[t],
        )[:4]
        b.lines.append(
            f"+{min(match.positive_weight, CAP_STRATEGY)} strategy overlap: {', '.join(top)}"
        )
    if match.negative_weight:
        negs = [t for t, w in match.matched.items() if w < 0]
        b.lines.append(f"{match.negative_weight} low-fit terms: {', '.join(negs)}")


def _score_complexity(facts: ManagerFacts, b: ScoreBreakdown) -> None:
    score = 0.0
    if facts.vehicle_count >= 2 or facts.adviser_fund_count >= 2:
        score += 8
        b.lines.append("+8 multiple related vehicles / private funds")
    if facts.has_master or facts.has_feeder:
        score += 6
        b.lines.append("+6 master/feeder structure")
    if facts.has_offshore:
        score += 6
        b.lines.append("+6 offshore structure")
    if facts.adviser_known and facts.adviser_fund_count >= 1:
        score += 5
        b.lines.append("+5 adviser footprint / platform growth")
    b.complexity = min(score, CAP_COMPLEXITY)


def _score_reachability(facts: ManagerFacts, b: ScoreBreakdown) -> None:
    score = 0.0
    if facts.identity_resolved:
        score += 6
        b.lines.append("+6 clear manager identity")
    if facts.has_known_buyers or facts.adviser_known:
        score += 5
        b.lines.append("+5 identifiable buyer roles / adviser mapping")
    if facts.has_outreach_path:
        score += 4
        b.lines.append("+4 mappable outreach path (web/HQ)")
    b.reachability = min(score, CAP_REACHABILITY)


def score_manager(facts: ManagerFacts, today: dt.date | None = None) -> ScoreBreakdown:
    today = today or dt.date.today()
    b = ScoreBreakdown()
    _score_event_strength(facts, today, b)
    _score_strategy_fit(facts, b)
    _score_complexity(facts, b)
    _score_reachability(facts, b)
    return b
