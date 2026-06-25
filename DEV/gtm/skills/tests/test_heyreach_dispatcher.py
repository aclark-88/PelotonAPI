"""heyreach_dispatcher: approval gate, dispatch flow, daily cap, idempotency."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from gtm.models.common import Channel
from gtm.models.funds import FundIn
from gtm.models.outreach import CampaignIn, DraftIn
from gtm.models.people import PersonIn
from gtm.models.signals import SignalIn
from gtm.skills import heyreach_dispatcher
from gtm.skills._shared.context import open_run
from gtm.skills.tests.conftest import VALID_CR, VALID_FOLLOWUP, FakeHeyReach, make_sources


def _seed(db, cleanup, run_suffix, with_followup=False, approve=True):
    fund = db.funds.upsert_fund(
        FundIn(legal_name=f"Dispatch Fund {run_suffix} LP", strategies=["macro"])
    )
    cleanup.append(("funds", str(fund.id)))
    person = db.people.upsert_person(
        PersonIn(
            full_name=f"Dispatch Target {run_suffix}",
            linkedin_url=f"https://linkedin.com/in/dispatch-{run_suffix}",
            current_fund_id=fund.id,
            current_role="COO",
        )
    )
    cleanup.append(("people", str(person.id)))
    signal = db.signals.record_signal(
        SignalIn(
            signal_type="new_fund_launch", source="manual",
            source_record_id=f"dispatch-{uuid.uuid4().hex}",
            observed_at=datetime.now(timezone.utc), fund_id=fund.id, payload={"t": 1},
        )
    )
    cleanup.append(("signals", str(signal.id)))
    campaign = db.outreach.upsert_campaign(
        CampaignIn(name=f"li-test-{run_suffix}", signal_type_key="new_fund_launch",
                   channel=Channel.linkedin)
    )
    cleanup.append(("campaigns", str(campaign.id)))
    draft = db.outreach.create_draft(
        DraftIn(person_id=person.id, signal_id=signal.id, campaign_id=campaign.id,
                channel=Channel.linkedin, variant_label="A", body=VALID_CR)
    )
    cleanup.append(("drafts", str(draft.id)))
    if with_followup:
        followup = db.outreach.create_draft(
            DraftIn(person_id=person.id, signal_id=signal.id, campaign_id=campaign.id,
                    channel=Channel.linkedin, variant_label="followup", body=VALID_FOLLOWUP)
        )
        cleanup.append(("drafts", str(followup.id)))
        db.outreach.approve_draft(followup.id, approved_by="alex")
    if approve:
        draft = db.outreach.approve_draft(draft.id, approved_by="alex")
    return person, campaign, draft


def test_unapproved_draft_refused(db, cleanup, run_suffix):
    _, _, draft = _seed(db, cleanup, run_suffix + "u", approve=False)
    heyreach = FakeHeyReach()

    with open_run("heyreach_dispatcher", sources=make_sources(heyreach=heyreach), db=db) as ctx:
        result = heyreach_dispatcher.run(ctx, draft_id=str(draft.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.metadata["dispatched"] is False
    assert any("not approved" in e["error"] for e in result.errors)
    assert getattr(heyreach, "added", []) == [], "no HeyReach call may happen for unapproved drafts"


def test_approved_dispatch_full_flow(db, cleanup, run_suffix):
    person, campaign, draft = _seed(db, cleanup, run_suffix + "d", with_followup=True)
    heyreach = FakeHeyReach()

    with open_run(
        "heyreach_dispatcher", sources=make_sources(heyreach=heyreach), db=db,
        config_overrides={"connections_per_day": 100000},
    ) as ctx:
        result = heyreach_dispatcher.run(ctx, draft_id=str(draft.id))
    cleanup.append(("source_runs", str(ctx.run_id)))
    if result.metadata.get("attempt_id"):
        cleanup.append(("outreach_attempts", result.metadata["attempt_id"]))

    assert result.metadata["dispatched"] is True
    assert result.metadata["followup_attached"] is True

    # canonical pattern: ONE existing campaign, lead appended with custom fields
    assert result.metadata["heyreach_campaign_id"] == 466941, "pinned canonical id"
    assert heyreach.campaigns == [], "must never create a per-prospect campaign"
    assert heyreach.lists == [], "must never create a per-prospect list"
    added = heyreach.added[0]
    assert added["campaign_id"] == 466941
    assert added["lead"]["profileUrl"] == person.linkedin_url
    # {{firstName}} MUST be resolved before sending — HeyReach won't substitute
    # a token nested inside a custom field value (verified-prod bug 2026-06-23).
    first = person.full_name.split()[0]
    assert "{{firstName}}" not in added["custom_fields"]["cr_note"]
    assert added["custom_fields"]["cr_note"] == VALID_CR.replace("{{firstName}}", first)
    assert "{{firstName}}" not in added["custom_fields"]["followup"]

    # attempt recorded + draft linked
    attempt = (
        db.client.table("outreach_attempts").select("*")
        .eq("id", result.metadata["attempt_id"]).single().execute()
    ).data
    assert attempt["status"] == "sent"
    assert attempt["external_id"] == str(result.metadata["heyreach_campaign_id"])
    assert attempt["channel"] == "linkedin"
    refreshed = (
        db.client.table("drafts").select("sent_attempt_id").eq("id", str(draft.id)).single().execute()
    ).data
    assert refreshed["sent_attempt_id"] == result.metadata["attempt_id"]

    # second dispatch of the same draft is a no-op
    with open_run("heyreach_dispatcher", sources=make_sources(heyreach=heyreach), db=db) as ctx2:
        second = heyreach_dispatcher.run(ctx2, draft_id=str(draft.id))
    cleanup.append(("source_runs", str(ctx2.run_id)))
    assert second.metadata.get("already_dispatched") is True
    assert len(heyreach.added) == 1, "no duplicate lead append"


def test_daily_cap_queues_instead_of_sending(db, cleanup, run_suffix):
    person, campaign, draft = _seed(db, cleanup, run_suffix + "c")
    heyreach = FakeHeyReach()

    with open_run(
        "heyreach_dispatcher", sources=make_sources(heyreach=heyreach), db=db,
        config_overrides={"connections_per_day": 0},
    ) as ctx:
        result = heyreach_dispatcher.run(ctx, draft_id=str(draft.id))
    cleanup.append(("source_runs", str(ctx.run_id)))

    assert result.metadata["dispatched"] is False
    assert result.metadata["queued"] is True
    assert getattr(heyreach, "added", []) == [], "capped dispatch must not touch HeyReach"

    queued = (
        db.client.table("outreach_attempts").select("*")
        .eq("person_id", str(person.id)).eq("campaign_id", str(campaign.id)).execute()
    ).data
    assert len(queued) == 1 and queued[0]["status"] == "queued"
    cleanup.append(("outreach_attempts", queued[0]["id"]))
