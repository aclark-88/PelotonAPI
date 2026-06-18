"""form_d_sweep: happy path, idempotency, dedupe, error path, ICP filtering."""

from __future__ import annotations

import pytest

from gtm.models.common import RunStatus
from gtm.skills import form_d_sweep
from gtm.skills._shared.context import open_run
from gtm.skills.tests.conftest import FakeEdgar, make_form_d, make_sources


def _track(cleanup, ctx, result):
    cleanup.append(("source_runs", str(ctx.run_id)))
    for sid in result.signals_emitted:
        cleanup.append(("signals", str(sid)))
    for fid in result.metadata.get("fund_ids", []):
        cleanup.append(("funds", fid))


def test_happy_path_emits_raise_and_launch(db, cleanup, fresh_cik, run_suffix):
    rec = make_form_d(fresh_cik, f"Test Macro Launch {run_suffix} LP")
    edgar = FakeEdgar(form_d=[rec], history_counts={fresh_cik: 1})

    with open_run("form_d_sweep", sources=make_sources(edgar=edgar), db=db) as ctx:
        result = form_d_sweep.run(ctx)
    _track(cleanup, ctx, result)

    assert result.status == "success"
    assert result.records_processed == 1
    assert result.records_inserted == 1
    assert len(result.signals_emitted) == 2, "capital_raise + new_fund_launch"

    fund = db.funds.find_by_cik(fresh_cik)
    assert fund is not None
    assert fund.is_emerging_manager is True
    types = {db.signals.get(s).signal_type for s in result.signals_emitted}
    assert types == {"capital_raise_form_d", "new_fund_launch"}
    # provenance: every signal carries this run's id
    for sid in result.signals_emitted:
        assert db.signals.get(sid).source_run_id == ctx.run_id


def test_idempotent_rerun_no_duplicates(db, cleanup, fresh_cik, run_suffix):
    rec = make_form_d(fresh_cik, f"Test Idempotent Fund {run_suffix} LP")
    edgar = FakeEdgar(form_d=[rec], history_counts={fresh_cik: 1})
    sources = make_sources(edgar=edgar)

    with open_run("form_d_sweep", sources=sources, db=db) as ctx1:
        first = form_d_sweep.run(ctx1)
    _track(cleanup, ctx1, first)

    with open_run("form_d_sweep", sources=sources, db=db) as ctx2:
        second = form_d_sweep.run(ctx2)
    cleanup.append(("source_runs", str(ctx2.run_id)))

    assert sorted(map(str, first.signals_emitted)) == sorted(map(str, second.signals_emitted)), \
        "re-run must dedupe to the same signal ids"
    funds = (
        db.client.table("funds").select("id", count="exact").eq("cik", fresh_cik).execute()
    )
    assert funds.count == 1, "re-run must not create a second fund"


def test_amendment_never_fires_launch(db, cleanup, fresh_cik, run_suffix):
    rec = make_form_d(
        fresh_cik, f"Test Amendment Fund {run_suffix} LP", amendment=True
    )
    edgar = FakeEdgar(form_d=[rec], history_counts={fresh_cik: 3})

    with open_run("form_d_sweep", sources=make_sources(edgar=edgar), db=db) as ctx:
        result = form_d_sweep.run(ctx)
    _track(cleanup, ctx, result)

    types = {db.signals.get(s).signal_type for s in result.signals_emitted}
    assert types == {"capital_raise_form_d"}


def test_icp_filters_skip_non_targets(db, cleanup, fresh_cik, run_suffix):
    records = [
        make_form_d(fresh_cik, f"Test PE Fund {run_suffix} LP", fund_type="Private Equity Fund"),
        make_form_d(str(int(fresh_cik) + 1), f"Test Real Estate Income {run_suffix} LLC"),
        make_form_d(str(int(fresh_cik) + 2), f"Tiny Fund {run_suffix} LP", offering=1_000_000),
        make_form_d(str(int(fresh_cik) + 3), f"Operating Co {run_suffix} Inc", industry_group="Other"),
    ]
    edgar = FakeEdgar(form_d=records)

    with open_run("form_d_sweep", sources=make_sources(edgar=edgar), db=db) as ctx:
        result = form_d_sweep.run(ctx)
    _track(cleanup, ctx, result)

    assert result.records_processed == 4
    assert result.signals_emitted == []
    skipped = result.metadata["skipped"]
    assert skipped["excluded_type"] == 1
    assert skipped["negative_term"] == 1
    assert skipped["below_min_offering"] == 1
    assert skipped["not_pooled_investment"] == 1


def test_error_path_marks_run_failed(db, cleanup):
    edgar = FakeEdgar(fail_on={"recent_form_d"})

    with pytest.raises(ConnectionError):
        with open_run("form_d_sweep", sources=make_sources(edgar=edgar), db=db) as ctx:
            form_d_sweep.run(ctx)
    cleanup.append(("source_runs", str(ctx.run_id)))

    row = (
        db.client.table("source_runs").select("*").eq("id", str(ctx.run_id)).single().execute()
    )
    assert row.data["status"] == RunStatus.failed.value
    assert row.data["ended_at"] is not None
    assert row.data["errors"], "failure must be recorded in source_runs.errors"
    signals = (
        db.client.table("signals")
        .select("id", count="exact")
        .eq("source_run_id", str(ctx.run_id))
        .execute()
    )
    assert signals.count == 0, "no partial signal writes on fetch failure"
