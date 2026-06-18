"""fund_verifier: human-verdict precedence, web verdicts, caching, write-back."""

from __future__ import annotations

import json

from gtm.models.funds import FundIn
from gtm.skills import fund_verifier
from gtm.skills._shared.context import load_config, open_run
from gtm.skills.tests.conftest import FakeEdgar, FakeWeb, make_search_result, make_sources
from gtm.skills._shared.sources import AdvProfile

CFG = load_config("fund_verifier")


def _seed(db, cleanup, run_suffix, name=None, cik=None):
    fund = db.funds.upsert_fund(
        FundIn(legal_name=name or f"Verify Fund {run_suffix} LP", cik=cik)
    )
    cleanup.append(("funds", str(fund.id)))
    return fund


def test_score_evidence_pure():
    hf = fund_verifier.score_evidence(
        ["Apex is a global macro hedge fund running relative value strategies"], CFG
    )
    assert hf["pos_score"] > 0 and hf["neg_score"] == 0
    assert "macro" in hf["strategy_hints"] and "relative_value" in hf["strategy_hints"]

    re_lender = fund_verifier.score_evidence(
        ["Octagon provides bridge loans for real estate and historic rehab properties,"
         " a private lender in direct lending"], CFG
    )
    assert re_lender["neg_score"] > re_lender["pos_score"]
    assert "real_estate" in re_lender["negatives_by_class"]
    assert "private_credit" in re_lender["negatives_by_class"]


def test_real_estate_lender_rejected(db, cleanup, run_suffix, fresh_cik):
    name = f"Octastyle Finance {run_suffix}"
    fund = _seed(db, cleanup, run_suffix, name=f"{name} LLC", cik=fresh_cik)
    web = FakeWeb(responses={
        name: [
            make_search_result(
                f"{name} - Real Estate Bridge Lending",
                f"https://example.com/octastyle-{run_suffix}",
                f"{name} is a private lender providing bridge loans for real estate "
                "rehab and historic tax credit properties. Direct lending for multifamily.",
            ),
        ]
    })

    with open_run("fund_verifier", sources=make_sources(web=web), db=db) as ctx:
        result = fund_verifier.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.metadata["is_hedge_fund"] is False
    assert "real estate" in result.metadata["business"].lower()
    refreshed = db.funds.get(fund.id)
    assert refreshed.metadata["verification"]["is_hedge_fund"] is False


def test_hedge_fund_confirmed_with_iapd_and_linkedin(db, cleanup, run_suffix, fresh_cik):
    name = f"Veritas Macro {run_suffix}"
    crd = f"V{fresh_cik[:7]}"
    fund = _seed(db, cleanup, run_suffix + "h", name=f"{name} LP", cik=fresh_cik)
    web = FakeWeb(responses={
        name: [
            make_search_result(
                f"{name} | LinkedIn",
                f"https://www.linkedin.com/company/veritas-{run_suffix}",
                f"{name} is a global macro hedge fund trading rates and FX, "
                "absolute return, founded by former Brevan Howard PM.",
            ),
        ]
    })
    edgar = FakeEdgar(adv={str(fresh_cik): AdvProfile(crd=crd, firm_name=f"{name} LP")})

    with open_run("fund_verifier", sources=make_sources(web=web, edgar=edgar), db=db) as ctx:
        result = fund_verifier.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.metadata["is_hedge_fund"] is True
    assert result.metadata["confidence"] >= 0.75
    assert "macro" in result.metadata["strategy_hints"]
    verification = db.funds.get(fund.id).metadata["verification"]
    assert verification["iapd_registered"] is True
    assert any("linkedin.com" in s for s in verification["sources"])

    # high confidence -> written back to the shared store (then clean it up)
    store_path = fund_verifier._store_path(CFG)
    store = json.loads(store_path.read_text(encoding="utf-8"))
    key = str(fresh_cik).lstrip("0")
    assert store[key]["is_target"] is True
    assert store[key]["verified_by"] == "gtm_auto"
    del store[key]
    store_path.write_text(json.dumps(store, indent=2), encoding="utf-8")


def test_human_verdict_wins_and_caches(db, cleanup, run_suffix, fresh_cik):
    fund = _seed(db, cleanup, run_suffix + "p", cik=fresh_cik)
    store_path = fund_verifier._store_path(CFG)
    store = json.loads(store_path.read_text(encoding="utf-8"))
    key = str(fresh_cik).lstrip("0")
    store[key] = {"is_target": False, "business": "Test human verdict - not a fund",
                  "verified_by": "web", "date": "2026-06-11"}
    store_path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    try:
        # human verdict beats web evidence (no web source even provided)
        with open_run("fund_verifier", sources=make_sources(), db=db) as ctx1:
            first = fund_verifier.run(ctx1, fund_id=str(fund.id))
        cleanup.append(("source_runs", str(ctx1.run_id)))
        assert first.metadata["is_hedge_fund"] is False
        assert first.metadata["confidence"] == 1.0

        # second run: cached on the fund row, no re-check
        with open_run("fund_verifier", sources=make_sources(), db=db) as ctx2:
            second = fund_verifier.run(ctx2, fund_id=str(fund.id))
        cleanup.append(("source_runs", str(ctx2.run_id)))
        assert second.metadata["cached"] is True
    finally:
        store = json.loads(store_path.read_text(encoding="utf-8"))
        store.pop(key, None)
        store_path.write_text(json.dumps(store, indent=2), encoding="utf-8")


def test_nothing_found_is_unverified(db, cleanup, run_suffix):
    fund = _seed(db, cleanup, run_suffix + "u")
    web = FakeWeb(responses={})

    with open_run("fund_verifier", sources=make_sources(web=web), db=db) as ctx:
        result = fund_verifier.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.metadata["is_hedge_fund"] is None
    assert "unverified" in result.metadata["business"]
    assert result.metadata["confidence"] == 0.0