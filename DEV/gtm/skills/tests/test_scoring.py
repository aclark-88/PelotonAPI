"""Pure unit tests for _shared/scoring.py — no DB, no network."""

from __future__ import annotations

from pathlib import Path

import yaml

from gtm.skills._shared.scoring import FitInputs, score_fund_fit, tier_for_score

ICP = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "configs" / "icp.yaml").read_text(encoding="utf-8")
)


def test_sweet_spot_macro_fund_scores_high():
    fit = score_fund_fit(
        FitInputs(
            aum_usd_millions=1500,
            strategies=["macro", "relative_value"],
            pct_private_fund=0.9,
            prime_broker_count=2,
            custodian_count=2,
            is_emerging_manager=True,
        ),
        ICP,
    )
    assert fit.score >= 75
    assert fit.tier == 1
    assert "aum_band" in fit.components and "strategy" in fit.components
    assert fit.reasoning


def test_out_of_icp_equity_fund_scores_low():
    fit = score_fund_fit(
        FitInputs(aum_usd_millions=8000, strategies=["equity_long_short"]),
        ICP,
    )
    assert fit.score < 35
    assert fit.tier == 4


def test_unknown_aum_uses_unknown_band():
    fit = score_fund_fit(FitInputs(strategies=["credit"]), ICP)
    assert fit.components["aum_band"] == ICP["scoring"]["aum_band_scores"]["unknown"]


def test_score_clamped_to_100():
    inflated = dict(ICP)
    inflated["scoring"] = {**ICP["scoring"], "emerging_manager_bonus": 500}
    fit = score_fund_fit(
        FitInputs(aum_usd_millions=1000, strategies=["macro"], is_emerging_manager=True),
        inflated,
    )
    assert fit.score == 100


def test_tier_boundaries():
    assert tier_for_score(75, ICP) == 1
    assert tier_for_score(74, ICP) == 2
    assert tier_for_score(55, ICP) == 2
    assert tier_for_score(54, ICP) == 3
    assert tier_for_score(35, ICP) == 3
    assert tier_for_score(34, ICP) == 4
