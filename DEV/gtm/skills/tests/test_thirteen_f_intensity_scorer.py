"""thirteen_f_intensity_scorer: scoring math, signal threshold, dedupe, absence."""

from __future__ import annotations

from gtm.models.funds import FundIn
from gtm.skills import thirteen_f_intensity_scorer as t13f
from gtm.skills._shared.context import open_run
from gtm.skills._shared.sources import ThirteenFSnapshot
from gtm.skills.tests.conftest import FakeEdgar, make_sources


def _seed_fund(db, cleanup, run_suffix, cik):
    fund = db.funds.upsert_fund(
        FundIn(legal_name=f"13F Test Fund {run_suffix}-{cik[-4:]} LP", cik=cik)
    )
    cleanup.append(("funds", str(fund.id)))
    return fund


def _snapshots(cik, options=40, positions=200):
    latest_names = [f"ISSUER{i}" for i in range(positions)]
    prior_names = [f"ISSUER{i}" for i in range(positions // 2, positions + positions // 2)]
    return [
        ThirteenFSnapshot(
            cik=cik, period="2026-03-31", position_count=positions,
            total_value_usd=2_000_000_000, option_position_count=options,
            top10_concentration=0.25, positions=latest_names,
        ),
        ThirteenFSnapshot(
            cik=cik, period="2025-12-31", position_count=positions,
            total_value_usd=1_800_000_000, option_position_count=options // 2,
            top10_concentration=0.30, positions=prior_names,
        ),
    ]


def test_high_intensity_emits_signal(db, cleanup, run_suffix, fresh_cik):
    fund = _seed_fund(db, cleanup, run_suffix, fresh_cik)
    edgar = FakeEdgar(thirteen_f={fresh_cik: _snapshots(fresh_cik)})

    with open_run("thirteen_f_intensity_scorer", sources=make_sources(edgar=edgar), db=db) as ctx:
        result = t13f.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))
    for sid in result.signals_emitted:
        cleanup.append(("signals", str(sid)))

    assert result.status == "success"
    assert result.metadata["intensity_score"] >= 60
    assert len(result.signals_emitted) == 1

    refreshed = db.funds.get(fund.id)
    meta = refreshed.metadata["thirteen_f"]
    assert meta["available"] is True
    assert meta["options_share"] == 0.2
    assert meta["intensity_score"] == result.metadata["intensity_score"]


def test_rerun_dedupes_on_period(db, cleanup, run_suffix, fresh_cik):
    fund = _seed_fund(db, cleanup, run_suffix, fresh_cik)
    edgar = FakeEdgar(thirteen_f={fresh_cik: _snapshots(fresh_cik)})
    sources = make_sources(edgar=edgar)

    with open_run("thirteen_f_intensity_scorer", sources=sources, db=db) as ctx1:
        first = t13f.run(ctx1, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx1.run_id)))
    for sid in first.signals_emitted:
        cleanup.append(("signals", str(sid)))

    with open_run("thirteen_f_intensity_scorer", sources=sources, db=db) as ctx2:
        second = t13f.run(ctx2, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx2.run_id)))

    assert [str(s) for s in second.signals_emitted] == [str(s) for s in first.signals_emitted]
    count = (
        db.client.table("signals")
        .select("id", count="exact")
        .eq("fund_id", str(fund.id))
        .eq("signal_type", "derivatives_intensity_high")
        .execute()
    )
    assert count.count == 1


def test_no_13f_records_unavailable(db, cleanup, run_suffix, fresh_cik):
    fund = _seed_fund(db, cleanup, run_suffix, fresh_cik)
    edgar = FakeEdgar(thirteen_f={})

    with open_run("thirteen_f_intensity_scorer", sources=make_sources(edgar=edgar), db=db) as ctx:
        result = t13f.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "success"
    assert result.metadata["thirteen_f_available"] is False
    assert result.signals_emitted == []
    refreshed = db.funds.get(fund.id)
    assert refreshed.metadata["thirteen_f"] == {"available": False, "reason": "no_filings"}


def test_low_intensity_no_signal(db, cleanup, run_suffix, fresh_cik):
    fund = _seed_fund(db, cleanup, run_suffix, fresh_cik)
    # almost no options, identical books (zero turnover), concentrated
    snaps = _snapshots(fresh_cik, options=0, positions=20)
    snaps[1].positions = snaps[0].positions
    snaps[0].top10_concentration = 0.95
    snaps[1].top10_concentration = 0.95
    edgar = FakeEdgar(thirteen_f={fresh_cik: snaps})

    with open_run("thirteen_f_intensity_scorer", sources=make_sources(edgar=edgar), db=db) as ctx:
        result = t13f.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.metadata["intensity_score"] < 60
    assert result.signals_emitted == []
