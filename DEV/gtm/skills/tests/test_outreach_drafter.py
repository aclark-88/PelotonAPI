"""outreach_drafter: validator, drafting flow, retry, LinkedIn-only output."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from gtm.models.funds import FundIn
from gtm.models.people import PersonIn
from gtm.models.signals import SignalIn
from gtm.skills import outreach_drafter
from gtm.skills._shared.context import load_config, open_run
from gtm.skills.tests.conftest import (
    VALID_CR,
    VALID_FOLLOWUP,
    FakeEmbedder,
    FakeLLM,
    make_sources,
)

CFG = load_config("outreach_drafter")


def test_angle_selection():
    s = outreach_drafter.select_angle
    assert s(["macro"], CFG)["key"] == "clarion_pms"
    assert s(["credit", "structured_credit"], CFG)["key"] == "clarion_pms"
    assert s(["equity_long_short"], CFG)["key"] == "network_value"
    assert s(["event_driven"], CFG)["key"] == "network_value"
    # mixed books with an ICP-core strategy stay on the Clarion play
    assert s(["equity_long_short", "multi_strategy"], CFG)["key"] == "clarion_pms"
    # unknown / empty falls back to default
    assert s([], CFG)["key"] == "clarion_pms"
    network = s(["equity_long_short"], CFG)
    assert "cap intro" in network["focus"] and "back office" in network["focus"]


def test_ls_equity_gets_network_angle(db, cleanup, run_suffix):
    fund = db.funds.upsert_fund(
        FundIn(legal_name=f"LS Launch {run_suffix} LP", strategies=["equity_long_short"],
               is_emerging_manager=True)
    )
    cleanup.append(("funds", str(fund.id)))
    person = db.people.upsert_person(
        PersonIn(full_name=f"LS Founder {run_suffix}",
                 linkedin_url=f"https://linkedin.com/in/ls-{run_suffix}",
                 current_fund_id=fund.id, current_role="Founder")
    )
    cleanup.append(("people", str(person.id)))
    signal = db.signals.record_signal(
        SignalIn(signal_type="new_fund_launch", source="manual",
                 source_record_id=f"ls-{uuid.uuid4().hex}",
                 observed_at=datetime.now(timezone.utc), fund_id=fund.id,
                 payload={"issuer": fund.legal_name})
    )
    cleanup.append(("signals", str(signal.id)))

    network_cr = (
        "Hi {{firstName}}, congrats on getting the fund off the ground. I work "
        "with launch-stage managers at Coremont and spend a lot of time around "
        "cap intro and outsourced back office decisions. Happy to make "
        "introductions where useful. Would value connecting as you build out."
    )
    llm = FakeLLM([json.dumps({"cr_variants": [network_cr, network_cr, network_cr],
                               "followup": VALID_FOLLOWUP})])

    with open_run("outreach_drafter", sources=make_sources(llm=llm), db=db) as ctx:
        result = outreach_drafter.run(ctx, person_id=str(person.id), signal_id=str(signal.id))
    cleanup.append(("source_runs", str(ctx.run_id)))
    for did in result.metadata["draft_ids"]:
        cleanup.append(("drafts", did))

    assert result.metadata["angle"] == "network_value"
    # the angle (with the cap-intro/back-office instruction) reached the prompt
    assert "network_value" in llm.calls[0][1] and "cap intro" in llm.calls[0][1]
    rows = (
        db.client.table("drafts").select("metadata").in_("id", result.metadata["draft_ids"]).execute()
    ).data
    assert all(r["metadata"]["angle"] == "network_value" for r in rows)


def test_validator_catches_violations():
    v = outreach_drafter.validate_linkedin_copy
    assert v(VALID_CR, CFG) == []
    assert any("chars" in x for x in v("x" * 301, CFG))
    assert any("dash" in x for x in v("Hi {{firstName}} — quick note", CFG))
    assert any("banned" in x for x in v("We can leverage our platform", CFG))
    # reciprocity leakage is hard-blocked (referral loop is internal doctrine)
    assert any("banned" in x for x in v("could be win-win for both of us", CFG))
    assert any("banned" in x for x in v("happy to help, and in return you could", CFG))
    assert any("banned" in x for x in v("you could introduce me to allocators", CFG))
    assert any("incumbent" in x for x in v("better than Geneva for your book", CFG))
    assert any("incumbent" in x for x in v("we integrate with Aladdin daily", CFG))
    # followup is not length-capped at 300
    assert v("y" * 400, CFG, is_cr=False) == []


def _seed_target(db, cleanup, run_suffix):
    fund = db.funds.upsert_fund(
        FundIn(legal_name=f"Drafter Fund {run_suffix} LP", strategies=["macro", "credit"],
               is_emerging_manager=True)
    )
    cleanup.append(("funds", str(fund.id)))
    person = db.people.upsert_person(
        PersonIn(
            full_name=f"Draft Target {run_suffix}",
            linkedin_url=f"https://linkedin.com/in/draft-{run_suffix}",
            current_fund_id=fund.id,
            current_role="Chief Operating Officer",
        )
    )
    cleanup.append(("people", str(person.id)))
    signal = db.signals.record_signal(
        SignalIn(
            signal_type="new_fund_launch",
            source="manual",
            source_record_id=f"drafttest-{uuid.uuid4().hex}",
            observed_at=datetime.now(timezone.utc),
            fund_id=fund.id,
            payload={"issuer": fund.legal_name, "declared_fund_type": "Hedge Fund"},
        )
    )
    cleanup.append(("signals", str(signal.id)))
    return fund, person, signal


def _good_llm_response():
    return json.dumps(
        {"cr_variants": [VALID_CR, VALID_CR.replace("global macro", "credit RV"),
                         VALID_CR.replace("consolidated risk", "intraday Greeks")],
         "followup": VALID_FOLLOWUP}
    )


def test_happy_path_drafts_linkedin_assets(db, cleanup, run_suffix):
    fund, person, signal = _seed_target(db, cleanup, run_suffix)
    llm = FakeLLM([_good_llm_response()])
    embedder = FakeEmbedder()

    with open_run("outreach_drafter", sources=make_sources(llm=llm, embedder=embedder), db=db) as ctx:
        result = outreach_drafter.run(ctx, person_id=str(person.id), signal_id=str(signal.id))
    cleanup.append(("source_runs", str(ctx.run_id)))
    for did in result.metadata["draft_ids"]:
        cleanup.append(("drafts", did))

    assert result.status == "success"
    assert len(result.metadata["draft_ids"]) == 4, "3 CR variants + 1 followup"
    assert result.metadata["llm_usage"]["output_tokens"] > 0

    rows = (
        db.client.table("drafts").select("*").in_("id", result.metadata["draft_ids"]).execute()
    ).data
    labels = sorted(r["variant_label"] for r in rows)
    assert labels == ["A", "B", "C", "followup"]
    for row in rows:
        assert row["channel"] == "linkedin"
        assert row["subject"] is None, "LinkedIn has no subject line"
        assert row["approved_at"] is None, "drafts must land unapproved — nothing sends"
        assert row["embedding"] is not None
        if row["variant_label"] != "followup":
            assert len(row["body"]) <= 300
            assert "{{firstName}}" in row["body"]
    # campaign auto-resolved from signal type mapping
    assert result.metadata["campaign_id"] is not None
    # voice + capabilities went into the prompt
    system = llm.calls[0][0]
    assert "VOICE CONTRACT" in system and "Nancy Tang" in system


def test_validation_retry_fixes_bad_variant(db, cleanup, run_suffix):
    fund, person, signal = _seed_target(db, cleanup, run_suffix + "r")
    bad = VALID_CR.replace("compare notes on", "leverage")  # banned phrase
    first = json.dumps({"cr_variants": [bad, VALID_CR, VALID_CR], "followup": VALID_FOLLOWUP})
    fix = json.dumps({"text": VALID_CR.replace("global macro", "rates RV")})
    llm = FakeLLM([first, fix])

    with open_run("outreach_drafter", sources=make_sources(llm=llm), db=db) as ctx:
        result = outreach_drafter.run(ctx, person_id=str(person.id), signal_id=str(signal.id))
    cleanup.append(("source_runs", str(ctx.run_id)))
    for did in result.metadata["draft_ids"]:
        cleanup.append(("drafts", did))

    assert len(result.metadata["draft_ids"]) == 4, "retry must rescue the bad variant"
    assert result.metadata["rejected"] == []
    assert len(llm.calls) == 2, "one drafting call + one corrective call"


def test_all_invalid_yields_no_drafts(db, cleanup, run_suffix):
    fund, person, signal = _seed_target(db, cleanup, run_suffix + "x")
    bad = "We leverage a cutting-edge solution better than Geneva — worth comparing notes?"
    response = json.dumps({"cr_variants": [bad, bad, bad], "followup": bad})
    llm = FakeLLM([response, json.dumps({"text": bad})])

    with open_run("outreach_drafter", sources=make_sources(llm=llm), db=db) as ctx:
        result = outreach_drafter.run(ctx, person_id=str(person.id), signal_id=str(signal.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.status == "partial"
    assert result.metadata["draft_ids"] == []
    assert result.metadata["rejected"]


def test_no_embedder_degrades_gracefully(db, cleanup, run_suffix):
    fund, person, signal = _seed_target(db, cleanup, run_suffix + "e")
    llm = FakeLLM([_good_llm_response()])

    with open_run("outreach_drafter", sources=make_sources(llm=llm), db=db) as ctx:
        result = outreach_drafter.run(ctx, person_id=str(person.id), signal_id=str(signal.id))
    cleanup.append(("source_runs", str(ctx.run_id)))
    for did in result.metadata["draft_ids"]:
        cleanup.append(("drafts", did))

    assert len(result.metadata["draft_ids"]) == 4
    rows = (
        db.client.table("drafts").select("embedding").in_("id", result.metadata["draft_ids"]).execute()
    ).data
    assert all(r["embedding"] is None for r in rows), "no OPENAI key -> drafts stored without embeddings"
