"""spinout_watcher: extraction heuristics, happy path, dedupe, error path."""

from __future__ import annotations

from gtm.skills import spinout_watcher
from gtm.skills._shared.context import open_run
from gtm.skills.tests.conftest import FakeWeb, make_search_result, make_sources

SUFFIXES = ["Capital", "Partners", "Management", "Capital Management"]


def test_extract_fund_candidate():
    text = "Veteran trader is launching Meridian Gate Capital Management after a decade at Citadel."
    assert spinout_watcher.extract_fund_candidate(text, SUFFIXES) == "Meridian Gate Capital Management"
    assert spinout_watcher.extract_fund_candidate("no fund here at all", SUFFIXES) is None


def test_extract_founder():
    text = "John Smallberries, a former Brevan Howard macro PM, is launching a new fund."
    assert spinout_watcher.extract_founder(text) == "John Smallberries"
    assert spinout_watcher.extract_founder("the fund led by Maria Chen targets credit") == "Maria Chen"


def _article(run_suffix, mothership="Brevan Howard"):
    return make_search_result(
        title=f"Ex-{mothership} PM launching Testspin{run_suffix} Capital Management",
        url=f"https://news.example.com/spinout-{run_suffix}",
        content=(
            f"Quentin Testfounder, a former {mothership} portfolio manager, is "
            f"launching Testspin{run_suffix} Capital Management, a global macro "
            f"spinout targeting $1bn."
        ),
        score=0.92,
    )


def _track(cleanup, ctx, result, db):
    cleanup.append(("source_runs", str(ctx.run_id)))
    for sid in result.signals_emitted:
        sig = db.signals.get(sid)
        cleanup.append(("signals", str(sid)))
        if sig.person_id:
            cleanup.append(("people", str(sig.person_id)))
        if sig.fund_id:
            cleanup.append(("funds", str(sig.fund_id)))


def test_happy_path_creates_candidate(db, cleanup, run_suffix):
    web = FakeWeb(responses={"Brevan Howard": [_article(run_suffix)]})

    with open_run("spinout_watcher", sources=make_sources(web=web), db=db) as ctx:
        result = spinout_watcher.run(ctx)
    _track(cleanup, ctx, result, db)

    assert result.status in ("success", "partial")
    assert len(result.signals_emitted) >= 1
    spin = next(
        s for s in (db.signals.get(i) for i in result.signals_emitted)
        if s.payload.get("mothership") == "Brevan Howard"
    )
    assert spin.signal_type == "spinout_detected"
    assert spin.urgency.value == "immediate"
    assert spin.payload["candidate_fund"] == f"Testspin{run_suffix} Capital Management"
    assert spin.payload["founder"] == "Quentin Testfounder"
    assert spin.fund_id is not None

    fund = db.funds.get(spin.fund_id)
    assert fund.is_emerging_manager is True
    person = db.people.get(spin.person_id)
    assert person.current_fund_id == fund.id
    assert person.current_role == "Founder"


def test_rerun_dedupes_same_urls(db, cleanup, run_suffix):
    web = FakeWeb(responses={"Brevan Howard": [_article(run_suffix)]})
    sources = make_sources(web=web)

    with open_run("spinout_watcher", sources=sources, db=db) as ctx1:
        first = spinout_watcher.run(ctx1)
    _track(cleanup, ctx1, first, db)

    with open_run("spinout_watcher", sources=sources, db=db) as ctx2:
        second = spinout_watcher.run(ctx2)
    cleanup.append(("source_runs", str(ctx2.run_id)))

    firsts = {str(s) for s in first.signals_emitted}
    seconds = {str(s) for s in second.signals_emitted}
    assert firsts == seconds, "same URLs must dedupe to the same signals"

    funds = db.funds.search_by_name_fuzzy(f"Testspin{run_suffix}")
    assert len(funds) == 1, "candidate fund must not duplicate on re-run"


def test_low_relevance_and_no_context_filtered(db, cleanup, run_suffix):
    noise = [
        make_search_result(
            title="Brevan Howard quarterly returns",
            url=f"https://news.example.com/returns-{run_suffix}",
            content="Brevan Howard posted gains last quarter.",  # no spinout context
            score=0.9,
        ),
        make_search_result(
            title=f"Random blog about Newfund {run_suffix} Capital spinout",
            url=f"https://blog.example.com/low-{run_suffix}",
            content=f"spinout chatter Brevan Howard Newfund {run_suffix} Capital",
            score=0.1,  # below floor
        ),
    ]
    web = FakeWeb(responses={"Brevan Howard": noise})

    with open_run("spinout_watcher", sources=make_sources(web=web), db=db) as ctx:
        result = spinout_watcher.run(ctx)
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.signals_emitted == []


def test_search_failure_is_partial_not_crash(db, cleanup):
    web = FakeWeb(fail=True)

    with open_run("spinout_watcher", sources=make_sources(web=web), db=db) as ctx:
        result = spinout_watcher.run(ctx)
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "partial"
    assert result.errors
    row = (
        db.client.table("source_runs").select("status, errors").eq("id", str(ctx.run_id)).single().execute()
    )
    assert row.data["status"] == "partial"
    assert row.data["errors"]

