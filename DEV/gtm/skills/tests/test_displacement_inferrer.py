"""displacement_inferrer: confidence model, signal payloads, dedupe, threshold."""

from __future__ import annotations

from gtm.models.funds import FundIn
from gtm.skills import displacement_inferrer
from gtm.skills._shared.context import open_run
from gtm.skills.tests.conftest import FakeWeb, make_search_result, make_sources


def _seed_fund(db, cleanup, run_suffix, tier=None):
    fund = db.funds.upsert_fund(
        FundIn(legal_name=f"Displace Test {run_suffix} LP", strategies=["credit"])
    )
    if tier:
        db.funds.record_fit_score(fund.id, 80, model_version="test", tier=tier)
    cleanup.append(("funds", str(fund.id)))
    return db.funds.get(fund.id)


def test_confidence_model_pure():
    hits = [
        {"url": "https://boards.greenhouse.io/x/jobs/1", "text": "experience with Advent Geneva required"},
        {"url": "https://news.example.com/a", "text": "fund uses Geneva"},
    ]
    cfg = {"per_hit": 0.25, "incumbency_phrase_bonus": 0.25, "careers_domain_bonus": 0.2}
    score = displacement_inferrer.score_vendor_hits(
        hits, cfg, ["experience with"], ["greenhouse.io"]
    )
    # hit1: 0.25 + 0.25 + 0.2 = 0.7; hit2: 0.25 -> 0.95
    assert abs(score - 0.95) < 1e-9
    assert displacement_inferrer.score_vendor_hits([], cfg, [], []) == 0.0


def _geneva_hits(run_suffix):
    name = f"Displace Test {run_suffix}"
    return [
        make_search_result(
            title=f"Operations Analyst at {name}",
            url=f"https://boards.greenhouse.io/displace{run_suffix}/jobs/123",
            content=f"{name} seeks an ops analyst. Experience with Advent Geneva required.",
        ),
        make_search_result(
            title=f"{name} middle office role",
            url=f"https://www.linkedin.com/jobs/view/displace-{run_suffix}",
            content=f"At {name} you will support of Advent Geneva accounting workflows.",
        ),
    ]


def test_happy_path_emits_signal_with_clarion_story(db, cleanup, run_suffix):
    fund = _seed_fund(db, cleanup, run_suffix, tier=2)
    web = FakeWeb(responses={"Advent Geneva": _geneva_hits(run_suffix)})

    with open_run("displacement_inferrer", sources=make_sources(web=web), db=db) as ctx:
        result = displacement_inferrer.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))
    for sid in result.signals_emitted:
        cleanup.append(("signals", str(sid)))

    assert result.status in ("success", "partial")
    vendors = {v["vendor"] for v in result.metadata["inferred_vendors"]}
    assert "Geneva" in vendors

    signal = next(
        db.signals.get(s) for s in result.signals_emitted
        if db.signals.get(s).payload.get("vendor") == "Geneva"
    )
    assert signal.signal_type == "displacement_inferred_job_post"
    assert signal.payload["confidence"] >= 0.3
    # the Clarion displacement story from clarion_coverage.yaml rides along
    assert "Geneva" in signal.payload["clarion_displacement"]
    assert "Operations Concierge" in signal.payload["clarion_displacement"]
    assert signal.urgency.value == "this_week", "tier-2 fund jumps the queue"

    refreshed = db.funds.get(fund.id)
    assert "Geneva" in refreshed.known_incumbent_pms
    assert refreshed.metadata["incumbent_vendors"][0]["vendor"] == "Geneva"
    assert result.metadata["adv_path_available"] is False


def test_rerun_dedupes(db, cleanup, run_suffix):
    fund = _seed_fund(db, cleanup, run_suffix + "d")
    web = FakeWeb(responses={"Advent Geneva": _geneva_hits(run_suffix + "d")})
    sources = make_sources(web=web)

    with open_run("displacement_inferrer", sources=sources, db=db) as ctx1:
        first = displacement_inferrer.run(ctx1, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx1.run_id)))
    for sid in first.signals_emitted:
        cleanup.append(("signals", str(sid)))

    with open_run("displacement_inferrer", sources=sources, db=db) as ctx2:
        second = displacement_inferrer.run(ctx2, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx2.run_id)))

    assert {str(s) for s in first.signals_emitted} == {str(s) for s in second.signals_emitted}
    count = (
        db.client.table("signals")
        .select("id", count="exact")
        .eq("fund_id", str(fund.id))
        .eq("signal_type", "displacement_inferred_job_post")
        .execute()
    )
    assert count.count == len(first.signals_emitted)


def test_weak_evidence_below_threshold_not_recorded(db, cleanup, run_suffix):
    fund = _seed_fund(db, cleanup, run_suffix + "w")
    name = f"Displace Test {run_suffix}w"
    weak = [
        make_search_result(
            title="Industry roundup",
            url=f"https://news.example.com/roundup-{run_suffix}",
            content=f"{name} mentioned alongside Enfusion in a market overview.",
        )
    ]
    web = FakeWeb(responses={"Enfusion": weak})

    with open_run("displacement_inferrer", sources=make_sources(web=web), db=db) as ctx:
        result = displacement_inferrer.run(ctx, fund_id=str(fund.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    # single non-job hit = 0.25 < 0.3 threshold
    assert result.metadata["inferred_vendors"] == []
    assert result.signals_emitted == []
    assert db.funds.get(fund.id).known_incumbent_pms == []
