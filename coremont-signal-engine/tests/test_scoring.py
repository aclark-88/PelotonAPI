import datetime as dt

from app import scoring
from app.scoring import FilingFacts, ManagerFacts


TODAY = dt.date(2026, 6, 3)


def _fresh_filing(days_ago=10, **kw):
    return FilingFacts(
        filing_date=TODAY - dt.timedelta(days=days_ago),
        first_sale_date=TODAY - dt.timedelta(days=days_ago),
        is_amendment=False,
        amount_sold=kw.get("amount_sold", 100_000_000),
        offering_amount=kw.get("offering_amount", 500_000_000),
    )


def test_tier_thresholds():
    assert scoring.tier_for_score(80) == 1
    assert scoring.tier_for_score(75) == 1
    assert scoring.tier_for_score(60) == 2
    assert scoring.tier_for_score(40) == 3
    assert scoring.tier_for_score(10) == 4


def test_event_strength_caps_at_30():
    facts = ManagerFacts(
        text="macro fund",
        filings=[
            _fresh_filing(5),
            FilingFacts(TODAY - dt.timedelta(days=3), None, is_amendment=True),
        ],
    )
    b = scoring.score_manager(facts, today=TODAY)
    # +18 new + +8 first sale + +4 amendment = 30, capped.
    assert b.event_strength == 30


def test_strong_strategy_fit_for_structured_credit_offshore_multi_vehicle():
    facts = ManagerFacts(
        text="Meridian Structured Credit Master Fund; Meridian Structured Credit Offshore Fund; macro rates",
        filings=[_fresh_filing(5)],
        vehicle_count=2,
        has_master=True,
        has_feeder=True,
        has_offshore=True,
        adviser_known=True,
        adviser_fund_count=2,
        has_outreach_path=True,
    )
    b = scoring.score_manager(facts, today=TODAY)
    assert b.strategy_fit == 30  # capped, plenty of strong overlap
    assert b.complexity == 25    # multi-vehicle + master/feeder + offshore + adviser, capped
    assert b.reachability >= 11
    assert b.tier == 1
    assert b.total >= 75
    # Every bucket should have produced an explanation line.
    assert any("strategy overlap" in line for line in b.lines)
    assert any("offshore" in line for line in b.lines)


def test_established_platform_gets_event_credit_and_reaches_tier1():
    # A mature multi-fund platform with no brand-new filing should still reach
    # Tier 1 on the strength of platform breadth + fit + complexity + reach.
    facts = ManagerFacts(
        text="AQR Multi-Strategy Fund; macro relative value structured credit",
        filings=[FilingFacts(TODAY - dt.timedelta(days=70), None, is_amendment=False)],
        vehicle_count=3,
        has_master=True,
        has_feeder=True,
        has_offshore=True,
        adviser_known=True,
        adviser_fund_count=3,
        has_known_buyers=True,
        has_outreach_path=True,
    )
    b = scoring.score_manager(facts, today=TODAY)
    assert b.event_strength >= 8  # platform credit fired despite stale filing
    assert any("multi-fund platform" in line for line in b.lines)
    assert b.tier == 1


def test_two_fund_platform_gets_smaller_event_credit():
    facts = ManagerFacts(text="credit fund", filings=[], vehicle_count=2)
    b = scoring.score_manager(facts, today=TODAY)
    assert b.event_strength == 4
    assert any("2 related funds" in line for line in b.lines)


def test_low_fit_manager_lands_low_tier():
    facts = ManagerFacts(
        text="Northpath Venture Growth Fund long-only equity",
        filings=[FilingFacts(TODAY - dt.timedelta(days=200), None, is_amendment=False)],
        vehicle_count=1,
        identity_resolved=True,
    )
    b = scoring.score_manager(facts, today=TODAY)
    assert b.event_strength == 0          # stale filing
    assert b.strategy_fit == 0            # negatives wipe out any fit
    assert b.tier >= 3


def test_strategy_fit_never_negative():
    facts = ManagerFacts(text="venture buyout long-only passive etf retail", filings=[])
    b = scoring.score_manager(facts, today=TODAY)
    assert b.strategy_fit == 0
