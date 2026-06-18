"""linkedin_signal_reader: proxy behavior, threshold signal, graceful absence."""

from __future__ import annotations

from gtm.models.funds import FundIn
from gtm.skills import linkedin_signal_reader
from gtm.skills._shared.context import open_run
from gtm.skills.tests.conftest import FakeWeb, make_search_result, make_sources


def _seed_fund(db, cleanup, run_suffix):
    fund = db.funds.upsert_fund(FundIn(legal_name=f"LISignal Test {run_suffix} LP"))
    cleanup.append(("funds", str(fund.id)))
    return fund


def _job_hits(run_suffix, count=3):
    name = f"LISignal Test {run_suffix}"
    return [
        make_search_result(
            title=f"{name} hiring role {i}",
            url=f"https://www.linkedin.com/jobs/view/li-{run_suffix}-{i}",
            content=f"{name} is hiring: operations and risk technology engineer, join the team.",
        )
        for i in range(count)
    ]


def test_hiring_velocity_signal(db, cleanup, run_suffix):
    fund = _seed_fund(db, cleanup, run_suffix)
    web = FakeWeb(
        responses={
            "hiring": _job_hits(run_suffix, 4),
            "site:linkedin.com": [
                make_search_result(
                    title=f"LISignal Test {run_suffix} post",
                    url=f"https://linkedin.com/posts/li-{run_suffix}",
                    content="We are growing.",
                )
            ],
        }
    )

    with open_run("linkedin_signal_reader", sources=make_sources(web=web), db=db) as ctx:
        result = linkedin_signal_reader.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))
    for sid in result.signals_emitted:
        cleanup.append(("signals", str(sid)))

    assert result.metadata["available"] is True
    assert result.metadata["job_postings"] == 4
    assert len(result.signals_emitted) == 1
    signal = db.signals.get(result.signals_emitted[0])
    assert signal.signal_type == "hiring_velocity_high"
    assert signal.payload["method"] == "web_search_proxy"
    assert signal.payload["posting_count"] == 4


def test_below_threshold_no_signal(db, cleanup, run_suffix):
    fund = _seed_fund(db, cleanup, run_suffix + "b")
    web = FakeWeb(responses={"hiring": _job_hits(run_suffix + "b", 2)})

    with open_run("linkedin_signal_reader", sources=make_sources(web=web), db=db) as ctx:
        result = linkedin_signal_reader.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.metadata["available"] is True
    assert result.signals_emitted == []


def test_nothing_indexed_reports_unavailable(db, cleanup, run_suffix):
    fund = _seed_fund(db, cleanup, run_suffix + "n")
    web = FakeWeb(responses={})

    with open_run("linkedin_signal_reader", sources=make_sources(web=web), db=db) as ctx:
        result = linkedin_signal_reader.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.metadata["available"] is False
    assert result.signals_emitted == []


def test_rerun_dedupes(db, cleanup, run_suffix):
    fund = _seed_fund(db, cleanup, run_suffix + "r")
    web = FakeWeb(responses={"hiring": _job_hits(run_suffix + "r", 3)})
    sources = make_sources(web=web)

    with open_run("linkedin_signal_reader", sources=sources, db=db) as ctx1:
        first = linkedin_signal_reader.run(ctx1, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx1.run_id)))
    for sid in first.signals_emitted:
        cleanup.append(("signals", str(sid)))

    with open_run("linkedin_signal_reader", sources=sources, db=db) as ctx2:
        second = linkedin_signal_reader.run(ctx2, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx2.run_id)))

    assert {str(s) for s in first.signals_emitted} == {str(s) for s in second.signals_emitted}
