"""Tests for the daily digest builder, new-flagging, and HTML rendering."""
import datetime as dt

import pytest

from app import digest as dg
from app.ingestion import formd, signal_job
from app.ingestion.pipeline import load_seed_records


@pytest.fixture(autouse=True)
def _clean_digest_state():
    """Each test starts with no prior digest state on disk (isolates first_run)."""
    dg._state_path().unlink(missing_ok=True)
    yield
    dg._state_path().unlink(missing_ok=True)


def _seed_and_score(session):
    formd.persist_records(session, load_seed_records())
    session.flush()
    signal_job.run(session, today=dt.date(2026, 6, 3))
    session.flush()


def test_digest_includes_only_tier1_and_tier2(session):
    _seed_and_score(session)
    d = dg.build_digest(session, min_tier=2)
    assert d.rows
    assert all(r.tier <= 2 for r in d.rows)
    assert d.tier1 >= 1 and d.tier2 >= 1
    # Sorted by score descending.
    scores = [r.score for r in d.rows]
    assert scores == sorted(scores, reverse=True)


def test_first_run_is_baseline_then_diff_flags_new(session):
    _seed_and_score(session)

    # First build: no prior state → baseline, nothing flagged new.
    d1 = dg.build_digest(session, min_tier=2)
    assert d1.first_run is True
    assert d1.new_count == 0
    dg.save_state(session, min_tier=2)

    # Second build with the same queue: not first run, still 0 new.
    d2 = dg.build_digest(session, min_tier=2)
    assert d2.first_run is False
    assert d2.new_count == 0

    # Simulate a brand-new manager entering the queue → it should flag as NEW.
    from app.models import Manager

    session.add(
        Manager(
            legal_name="Newcomer Macro Fund LP",
            normalized_name="newcomer macro",
            total_score=80.0,
            tier=1,
            strategy_tags=["macro"],
            last_signal_date=dt.date(2026, 6, 3),
        )
    )
    session.flush()
    d3 = dg.build_digest(session, min_tier=2)
    new_names = [r.name for r in d3.rows if r.is_new]
    assert "Newcomer Macro Fund LP" in new_names
    assert d3.new_count == 1


def test_render_html_is_self_contained_and_has_content(session):
    _seed_and_score(session)
    d = dg.build_digest(session, min_tier=2)
    html = dg.render_html(d)
    assert "Coremont" in html
    assert "Meridian Structured Credit Master Fund LP" in html
    # Email-safe: no external stylesheet / script tags.
    assert "<link" not in html and "<script" not in html
    # Plain-text fallback present and informative.
    text = dg.render_text(d)
    assert "Tier 1" in text and "Meridian" in text


def test_subject_summarizes_counts(session):
    _seed_and_score(session)
    d = dg.build_digest(session, min_tier=2)
    subj = dg.subject(d)
    assert "Coremont Signal Engine" in subj
    assert "Tier 1" in subj and "Tier 2" in subj
