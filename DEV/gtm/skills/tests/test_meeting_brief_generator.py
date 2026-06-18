"""meeting_brief_generator: assembly, talking points, objections, degradation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from gtm.models.common import RoleFunction, Seniority
from gtm.models.funds import FundIn
from gtm.models.people import PersonIn
from gtm.models.signals import SignalIn
from gtm.skills import meeting_brief_generator
from gtm.skills._shared.context import open_run
from gtm.skills.tests.conftest import FakeHubSpot, FakeSlack, make_sources


def _seed(db, cleanup, run_suffix, tmp_path):
    fund = db.funds.upsert_fund(
        FundIn(
            legal_name=f"Brief Fund {run_suffix} LP", strategies=["macro"],
            is_emerging_manager=True, known_incumbent_pms=["Geneva"],
        )
    )
    cleanup.append(("funds", str(fund.id)))
    person = db.people.upsert_person(
        PersonIn(
            full_name=f"Brief Coo {run_suffix}",
            linkedin_url=f"https://linkedin.com/in/brief-{run_suffix}",
            current_fund_id=fund.id, current_role="COO",
            current_role_function=RoleFunction.ops, current_role_seniority=Seniority.c_suite,
        )
    )
    cleanup.append(("people", str(person.id)))
    signal = db.signals.record_signal(
        SignalIn(
            signal_type="new_fund_launch", source="manual",
            source_record_id=f"brief-{uuid.uuid4().hex}",
            observed_at=datetime.now(timezone.utc), fund_id=fund.id,
            payload={"issuer": fund.legal_name},
        )
    )
    cleanup.append(("signals", str(signal.id)))
    return fund, person


def test_brief_assembly_full(db, cleanup, run_suffix, tmp_path):
    fund, person = _seed(db, cleanup, run_suffix, tmp_path)
    hubspot = FakeHubSpot(
        won=[{"id": "d1", "properties": {"dealname": "Won Macro Fund", "closedate": "2026-04-01", "amount": "250000"}}]
    )
    slack = FakeSlack()

    with open_run(
        "meeting_brief_generator",
        sources=make_sources(hubspot=hubspot, slack=slack), db=db,
        config_overrides={"output_dir": str(tmp_path)},
    ) as ctx:
        result = meeting_brief_generator.run(
            ctx, fund_id=str(fund.id), person_ids=[str(person.id)]
        )
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "success"
    brief = Path(result.metadata["brief_path"]).read_text(encoding="utf-8")

    assert f"# Meeting brief — Brief Fund {run_suffix} LP" in brief
    assert "new_fund_launch" in brief, "signal table present"
    assert f"Brief Coo {run_suffix}" in brief and "Buying committee: True" in brief
    assert "Geneva (confidence: known)" in brief and "do not name first" in brief
    assert "Launch infrastructure" in brief, "talking point keyed to new_fund_launch"
    assert "can't afford institutional infrastructure" in brief, "emerging-manager objection selected"
    assert "Won Macro Fund" in brief, "closed-won from HubSpot included"
    assert slack.posts and "Meeting brief ready" in slack.posts[0]


def test_no_slack_no_hubspot_degrades(db, cleanup, run_suffix, tmp_path):
    fund, person = _seed(db, cleanup, run_suffix + "d", tmp_path)

    with open_run(
        "meeting_brief_generator", sources=make_sources(), db=db,
        config_overrides={"output_dir": str(tmp_path)},
    ) as ctx:
        result = meeting_brief_generator.run(
            ctx, fund_id=str(fund.id), person_ids=[str(person.id)]
        )
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "success", "missing slack/hubspot must not fail the brief"
    brief = Path(result.metadata["brief_path"]).read_text(encoding="utf-8")
    assert "_None available._" in brief, "won-deals section degrades honestly"


def test_unknown_fund_is_clean_error(db, cleanup, tmp_path):
    with open_run(
        "meeting_brief_generator", sources=make_sources(), db=db,
        config_overrides={"output_dir": str(tmp_path)},
    ) as ctx:
        result = meeting_brief_generator.run(ctx, fund_id=str(uuid.uuid4()))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "partial"
    assert "not resolved" in result.errors[0]["error"]
