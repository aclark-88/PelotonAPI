"""Template copy: every variant passes the validator and length cap."""

from __future__ import annotations

from gtm.skills._shared import templates
from gtm.skills._shared.context import load_config
from gtm.skills.outreach_drafter import validate_linkedin_copy

CFG = load_config("outreach_drafter")

CASES = [
    (["structured_credit"], "finance", "clarion_pms"),
    (["structured_credit"], "ops", "clarion_pms"),
    (["structured_credit"], "tech", "clarion_pms"),
    (["macro"], "investment", "clarion_pms"),
    (["credit"], "executive", "clarion_pms"),
    (["multi_strategy"], "risk", "clarion_pms"),
    ([], "unknown", "clarion_pms"),
    (["equity_long_short"], "investment", "network_value"),
    (["event_driven"], "finance", "network_value"),
]


def test_all_templates_valid():
    for strategies, fn, angle in CASES:
        copy = templates.build_copy(strategies, fn, angle)
        cr = copy["cr_variants"][0]
        fu = copy["followup"]
        assert len(cr) <= 300, f"{angle}/{fn}/{strategies}: CR {len(cr)} chars"
        assert validate_linkedin_copy(cr, CFG, is_cr=True) == [], f"{angle}/{fn} CR violations"
        assert validate_linkedin_copy(fu, CFG, is_cr=False) == [], f"{angle}/{fn} FU violations"
        assert "{{firstName}}" in cr


def test_network_offer_menu():
    copy = templates.build_copy(["equity_long_short"], "investment", "network_value")
    cr = copy["cr_variants"][0]
    assert "cap intro" in cr
    assert "CFO/CTO" in cr or "CFO" in cr
    assert "infrastructure" in cr
    assert "Clarion" not in cr  # network play never pitches the product


def test_clarion_tech_gets_api():
    cr = templates.build_copy(["credit"], "tech", "clarion_pms")["cr_variants"][0]
    assert "Clarion API" in cr


def test_strategy_phrase_fallback():
    assert templates.strategy_phrase(["structured_credit"]) == "structured credit"
    assert templates.strategy_phrase(["unknownthing"]) == "multi-asset"
    assert templates.strategy_phrase([]) == "multi-asset"
