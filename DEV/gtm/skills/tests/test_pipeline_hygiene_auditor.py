"""pipeline_hygiene_auditor: flag logic, weekly dedupe, report, degradation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gtm.models.funds import FundIn
from gtm.models.people import PersonIn
from gtm.models.signals import SignalIn
from gtm.skills import pipeline_hygiene_auditor
from gtm.skills._shared.context import open_run
from gtm.skills.tests.conftest import FakeHubSpot, FakeSlack, make_sources


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _healthy_deal(deal_id: str, name: str):
    now = datetime.now(timezone.utc)
    return {
        "id": deal_id,
        "properties": {
            "dealname": name, "dealstage": "qualified",
            "notes_last_updated": _iso(now - timedelta(days=2)),
            "hs_next_step": "demo scheduled",
        },
    }


def _broken_deal(deal_id: str, name: str):
    now = datetime.now(timezone.utc)
    return {
        "id": deal_id,
        "properties": {
            "dealname": name, "dealstage": "negotiation",
            "notes_last_updated": _iso(now - timedelta(days=45)),
            "hs_next_step": "",
        },
    }


def test_flags_and_signals(db, cleanup, run_suffix, tmp_path):
    domain = f"hygiene-{run_suffix}.com"
    fund = db.funds.upsert_fund(
        FundIn(legal_name=f"Hygiene Fund {run_suffix} LP", primary_domain=domain,
               strategies=["credit"])
    )
    cleanup.append(("funds", str(fund.id)))
    # fresh signal on the fund -> signals_decayed must NOT fire
    sig = db.signals.record_signal(
        SignalIn(signal_type="manual_flag", source="manual",
                 source_record_id=f"hyg-{uuid.uuid4().hex}",
                 observed_at=datetime.now(timezone.utc), fund_id=fund.id, payload={"x": 1})
    )
    cleanup.append(("signals", str(sig.id)))
    # a contact who has LEFT the fund (current_fund_id elsewhere)
    other = db.funds.upsert_fund(FundIn(legal_name=f"Elsewhere {run_suffix} LP"))
    cleanup.append(("funds", str(other.id)))
    mover = db.people.upsert_person(
        PersonIn(full_name=f"Gone Person {run_suffix}",
                 email=f"gone-{run_suffix}@{domain}", current_fund_id=other.id)
    )
    cleanup.append(("people", str(mover.id)))

    deal_id = f"deal-{run_suffix}"
    hubspot = FakeHubSpot(
        deals=[_broken_deal(deal_id, f"Hygiene Deal {run_suffix}"),
               _healthy_deal(f"ok-{run_suffix}", f"Healthy Deal {run_suffix}")],
        companies={"c1": {"id": "c1", "properties": {"name": f"Hygiene Fund {run_suffix}", "domain": domain}}},
        crm_contacts={"p1": {"id": "p1", "properties": {"email": f"gone-{run_suffix}@{domain}"}}},
        associations={
            (deal_id, "companies"): ["c1"],
            (deal_id, "contacts"): ["p1"],
            (f"ok-{run_suffix}", "companies"): ["c1"],
            (f"ok-{run_suffix}", "contacts"): [],
        },
    )
    slack = FakeSlack()

    with open_run(
        "pipeline_hygiene_auditor", sources=make_sources(hubspot=hubspot, slack=slack),
        db=db, config_overrides={"output_dir": str(tmp_path)},
    ) as ctx:
        result = pipeline_hygiene_auditor.run(ctx)
    cleanup.append(("source_runs", str(ctx.run_id)))
    for sid in result.signals_emitted:
        cleanup.append(("signals", str(sid)))

    assert result.metadata["open_deals"] == 2
    flagged = {f["name"]: f["flags"] for f in result.metadata["flagged"]}
    assert f"Hygiene Deal {run_suffix}" in flagged
    assert set(flagged[f"Hygiene Deal {run_suffix}"]) == {"stale", "no_next_step", "contact_left"}
    assert f"Healthy Deal {run_suffix}" not in flagged, "healthy deal must not be flagged"

    # one opp_at_risk for the broken deal
    assert len(result.signals_emitted) == 1
    risk = db.signals.get(result.signals_emitted[0])
    assert risk.signal_type == "opp_at_risk"
    assert "contact_left" in risk.payload["flags"]
    assert risk.payload["detail"]["departed"] == [f"Gone Person {run_suffix}"]

    report = Path(result.metadata["report_path"]).read_text(encoding="utf-8")
    assert "## Stale" in report and "## Contact left the company" in report
    assert f"departed: Gone Person {run_suffix}" in report
    assert slack.posts and "1/2 deals flagged" in slack.posts[0]


def test_weekly_dedupe_on_rerun(db, cleanup, run_suffix, tmp_path):
    deal_id = f"dedupe-{run_suffix}"
    hubspot = FakeHubSpot(deals=[_broken_deal(deal_id, f"Dedupe Deal {run_suffix}")])
    sources = make_sources(hubspot=hubspot)

    with open_run("pipeline_hygiene_auditor", sources=sources, db=db,
                  config_overrides={"output_dir": str(tmp_path)}) as ctx1:
        first = pipeline_hygiene_auditor.run(ctx1)
    cleanup.append(("source_runs", str(ctx1.run_id)))
    for sid in first.signals_emitted:
        cleanup.append(("signals", str(sid)))

    with open_run("pipeline_hygiene_auditor", sources=sources, db=db,
                  config_overrides={"output_dir": str(tmp_path)}) as ctx2:
        second = pipeline_hygiene_auditor.run(ctx2)
    cleanup.append(("source_runs", str(ctx2.run_id)))

    assert [str(s) for s in first.signals_emitted] == [str(s) for s in second.signals_emitted], \
        "same deal, same week -> same opp_at_risk signal"


def test_all_clear_report(db, cleanup, run_suffix, tmp_path):
    hubspot = FakeHubSpot(deals=[])

    with open_run("pipeline_hygiene_auditor", sources=make_sources(hubspot=hubspot),
                  db=db, config_overrides={"output_dir": str(tmp_path)}) as ctx:
        result = pipeline_hygiene_auditor.run(ctx)
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "success"
    report = Path(result.metadata["report_path"]).read_text(encoding="utf-8")
    assert "All clear" in report


def test_no_hubspot_is_clean_error(db, cleanup, tmp_path):
    with open_run("pipeline_hygiene_auditor", sources=make_sources(), db=db,
                  config_overrides={"output_dir": str(tmp_path)}) as ctx:
        result = pipeline_hygiene_auditor.run(ctx)
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "partial"
    assert "hubspot" in result.errors[0]["error"]