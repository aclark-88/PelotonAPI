"""adv_fit_scorer: enrichment, scoring, signal-on-delta, dedupe, graceful absence."""

from __future__ import annotations

from gtm.models.funds import FundIn
from gtm.skills import adv_fit_scorer
from gtm.skills._shared.context import open_run
from gtm.skills._shared.sources import AdvProfile
from gtm.skills.tests.conftest import FakeEdgar, make_sources


def _seed_fund(db, cleanup, run_suffix, crd, **kwargs):
    fund = db.funds.upsert_fund(
        FundIn(legal_name=f"ADV Test Fund {run_suffix}-{crd} LP", crd=crd, **kwargs)
    )
    cleanup.append(("funds", str(fund.id)))
    return fund


def _profile(crd, aum_millions=1500.0, pct_private=0.9):
    return AdvProfile(
        crd=crd,
        firm_name="ADV Test Global Macro Management",
        regulatory_aum_usd=aum_millions,
        aum_as_of="2026-03-31",
        pct_private_fund=pct_private,
        prime_brokers=["Goldman Sachs", "Morgan Stanley"],
        custodians=["BNY", "State Street"],
        administrator="Citco",
    )


def test_happy_path_enriches_and_scores(db, cleanup, run_suffix, fresh_cik):
    crd = f"T{fresh_cik[:8]}"
    fund = _seed_fund(db, cleanup, run_suffix, crd, strategies=["macro"], is_emerging_manager=True)
    edgar = FakeEdgar(adv={crd: _profile(crd)})

    with open_run("adv_fit_scorer", sources=make_sources(edgar=edgar), db=db) as ctx:
        result = adv_fit_scorer.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "success"
    assert result.metadata["adv_available"] is True

    refreshed = db.funds.get(fund.id)
    assert refreshed.aum_usd_millions == 1500.0
    assert refreshed.aum_band == "1b_to_5b"
    assert refreshed.prime_brokers == ["Goldman Sachs", "Morgan Stanley"]
    assert refreshed.administrator == "Citco"
    assert refreshed.fit_score == result.metadata["fit_score"]
    assert refreshed.tier == result.metadata["tier"]
    # macro + $1.5B + multi-PB + multi-custodian + emerging should be tier 1-2
    assert refreshed.tier in (1, 2)

    runs = (
        db.client.table("scoring_runs")
        .select("*")
        .eq("entity_id", str(fund.id))
        .eq("entity_type", "fund")
        .execute()
    )
    assert len(runs.data) == 1
    cleanup.append(("scoring_runs", runs.data[0]["id"]))
    assert runs.data[0]["reasoning"]


def test_signal_on_material_delta_and_dedupe(db, cleanup, run_suffix, fresh_cik):
    crd = f"T{fresh_cik[:8]}D"
    fund = _seed_fund(db, cleanup, run_suffix, crd, strategies=["macro"])
    # establish a prior, very low cached score
    db.funds.record_fit_score(fund.id, 5, model_version="fit-test-prior")
    edgar = FakeEdgar(adv={crd: _profile(crd)})
    sources = make_sources(edgar=edgar)

    with open_run("adv_fit_scorer", sources=sources, db=db) as ctx1:
        first = adv_fit_scorer.run(ctx1, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx1.run_id)))
    for sid in first.signals_emitted:
        cleanup.append(("signals", str(sid)))
    assert len(first.signals_emitted) == 1, "delta from 5 must emit fit_score_changed"
    sig = db.signals.get(first.signals_emitted[0])
    assert sig.signal_type == "fit_score_changed"
    assert sig.payload["old_score"] == 5

    # second run same day: score unchanged -> delta 0 -> no new signal
    with open_run("adv_fit_scorer", sources=sources, db=db) as ctx2:
        second = adv_fit_scorer.run(ctx2, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx2.run_id)))
    assert second.signals_emitted == []

    count = (
        db.client.table("signals")
        .select("id", count="exact")
        .eq("fund_id", str(fund.id))
        .eq("signal_type", "fit_score_changed")
        .execute()
    )
    assert count.count == 1

    # scoring history is append-only: prior + two runs = 3 rows
    runs = (
        db.client.table("scoring_runs")
        .select("id")
        .eq("entity_id", str(fund.id))
        .execute()
    )
    assert len(runs.data) == 3
    for r in runs.data:
        cleanup.append(("scoring_runs", r["id"]))


def test_adv_not_found_is_graceful(db, cleanup, run_suffix, fresh_cik):
    fund = _seed_fund(db, cleanup, run_suffix, f"T{fresh_cik[:8]}X")
    edgar = FakeEdgar(adv={})  # nothing matches

    with open_run("adv_fit_scorer", sources=make_sources(edgar=edgar), db=db) as ctx:
        result = adv_fit_scorer.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "success"
    assert result.metadata["adv_available"] is False
    assert db.funds.get(fund.id).fit_score is None, "no score without ADV data"


def test_error_path_records_failure(db, cleanup, run_suffix, fresh_cik):
    import pytest

    fund = _seed_fund(db, cleanup, run_suffix, f"T{fresh_cik[:8]}E")
    edgar = FakeEdgar(fail_on={"adv_firm_profile"})

    with pytest.raises(ConnectionError):
        with open_run("adv_fit_scorer", sources=make_sources(edgar=edgar), db=db) as ctx:
            adv_fit_scorer.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    row = (
        db.client.table("source_runs").select("status").eq("id", str(ctx.run_id)).single().execute()
    )
    assert row.data["status"] == "failed"
