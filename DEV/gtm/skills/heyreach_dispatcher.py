"""heyreach_dispatcher — push one APPROVED LinkedIn draft to HeyReach.

This is the system's only send path (LinkedIn-only campaign; there is no
email dispatcher). Tier-4 boundary holds: dispatch requires a human-approved
draft (approved_at set by a person via OutreachRepo.approve_draft) — this
skill refuses anything else.

CANONICAL-CAMPAIGN PATTERN (Alex, 2026-06-11): every lead appends to ONE
HeyReach campaign (configs/heyreach_dispatcher.yaml canonical_campaign_id;
created by gtm/cli/setup_heyreach.py). Per-lead copy travels as
customUserFields: cr_note fills the CR template, followup fills the
post-accept MESSAGE. If no approved followup exists, the field is omitted and
the campaign's variable-free fallback text sends instead. One campaign =
consistent tracking, one sender, one schedule, no orphans.

Flow:
  1. verify draft approved + channel linkedin + person has linkedin_url
  2. enforce the per-day connection cap (attempts stay 'queued' past the cap)
  3. append the lead to the canonical campaign with custom fields
  4. record outreach_attempts (step 1, status=sent, external_id =
     canonical campaign id, profile url in metadata) + link the draft

Idempotent: draft.sent_attempt_id blocks re-dispatch of the same draft; the
unique (person, campaign, step) constraint blocks duplicate sends; HeyReach's
own exclude-contacted setting is the third net.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from gtm.models.common import Channel, OutreachStatus
from gtm.models.outreach import OutreachAttemptIn
from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.sources import SourceUnavailable

SKILL_NAME = "heyreach_dispatcher"


def _sent_today(ctx: SkillContext) -> int:
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    resp = (
        ctx.db.client.table("outreach_attempts")
        .select("id", count="exact")
        .eq("channel", "linkedin")
        .gte("sent_at", midnight.isoformat())
        .execute()
    )
    return resp.count or 0


def _approved_followup(ctx: SkillContext, draft) -> str | None:
    resp = (
        ctx.db.client.table("drafts")
        .select("body, approved_at")
        .eq("person_id", str(draft.person_id))
        .eq("variant_label", "followup")
        .eq("channel", "linkedin")
        .is_("deleted_at", "null")
        .not_.is_("approved_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0]["body"] if resp.data else None


def run(ctx: SkillContext, draft_id: str) -> SkillResult:
    draft_row = (
        ctx.db.client.table("drafts").select("*").eq("id", str(draft_id)).limit(1).execute()
    )
    if not draft_row.data:
        ctx.result.error("resolve", f"draft {draft_id} not found")
        return ctx.result.build()
    from gtm.models.outreach import Draft

    draft = Draft.model_validate(draft_row.data[0])
    ctx.result.records_processed = 1

    # ── gates ────────────────────────────────────────────────────────────────
    if draft.approved_at is None:
        ctx.result.error("approval", "draft is not approved — refusing to dispatch (Tier-4 boundary)")
        return ctx.result.build(dispatched=False)
    if draft.channel != Channel.linkedin:
        ctx.result.error("channel", f"draft channel is {draft.channel}, this campaign is LinkedIn-only")
        return ctx.result.build(dispatched=False)
    if draft.sent_attempt_id is not None:
        ctx.logger.info("already_dispatched", draft=str(draft.id))
        return ctx.result.build(dispatched=False, already_dispatched=True)
    if draft.campaign_id is None:
        ctx.result.error("campaign", "draft has no campaign_id")
        return ctx.result.build(dispatched=False)

    person = ctx.db.people.get(draft.person_id)
    if person is None or not person.linkedin_url:
        ctx.result.error("resolve", "person missing or has no linkedin_url")
        return ctx.result.build(dispatched=False)

    # ── daily cap ────────────────────────────────────────────────────────────
    cap_cfg = ctx.config.get("connections_per_day")
    if cap_cfg is None:  # explicit: 0 is a valid cap, `or` would swallow it
        cap_cfg = (ctx.config.get("heyreach") or {}).get("connections_per_day_per_seat", 25)
    cap = int(cap_cfg)
    if _sent_today(ctx) >= cap:
        if not ctx.dry_run:
            attempt = ctx.db.outreach.record_attempt(
                OutreachAttemptIn(
                    person_id=draft.person_id,
                    campaign_id=draft.campaign_id,
                    signal_id=draft.signal_id,
                    channel=Channel.linkedin,
                    step_number=1,
                    status=OutreachStatus.queued,
                    draft_id=draft.id,
                ),
                source_run_id=ctx.run_id,
            )
            ctx.result.records_inserted += 1
        ctx.logger.info("daily_cap_hit", cap=cap)
        return ctx.result.build(dispatched=False, queued=True, cap=cap)

    try:
        heyreach = ctx.sources.require("heyreach")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build(dispatched=False)

    defaults = ctx.config.get("defaults", {})
    sender = heyreach.find_sender(str(defaults.get("sender_keyword", "alex")))
    if sender is None:
        ctx.result.error("sender", "no LinkedIn sender account matched sender_keyword")
        return ctx.result.build(dispatched=False)

    # ── canonical campaign (id pinned in config; name lookup as fallback) ────
    canonical_id = ctx.config.get("canonical_campaign_id")
    if not canonical_id:
        found = heyreach.find_campaign(
            str(ctx.config.get("canonical_campaign_name", "Clarion GTM - Launch Outreach"))
        )
        canonical_id = found.get("id") if found else None
    if not canonical_id:
        ctx.result.error("campaign", "canonical HeyReach campaign not found — run gtm/cli/setup_heyreach.py")
        return ctx.result.build(dispatched=False)
    canonical_id = int(canonical_id)

    followup = _approved_followup(ctx, draft) if ctx.config.get("attach_followup", True) else None
    fund = ctx.db.funds.get(person.current_fund_id) if person.current_fund_id else None

    if ctx.dry_run:
        ctx.logger.info("dry_run_dispatch", person=person.full_name, campaign=canonical_id)
        return ctx.result.build(dispatched=False, dry_run=True)

    # ── append to the canonical campaign ─────────────────────────────────────
    name_parts = person.full_name.split()
    lead = {
        "profileUrl": person.linkedin_url,
        "firstName": name_parts[0],
        "lastName": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
        "companyName": (fund.common_name or fund.legal_name) if fund else "",
        "position": person.current_role or "",
    }
    custom_fields = {"cr_note": draft.body}
    if followup:
        custom_fields["followup"] = followup
    # no approved followup -> omit the field; the campaign's variable-free
    # fallback message sends on accept instead of an empty bubble
    heyreach.add_leads_to_campaign(canonical_id, int(sender["id"]), lead, custom_fields)

    attempt = ctx.db.outreach.record_attempt(
        OutreachAttemptIn(
            person_id=draft.person_id,
            campaign_id=draft.campaign_id,
            signal_id=draft.signal_id,
            channel=Channel.linkedin,
            step_number=1,
            sent_at=datetime.now(timezone.utc),
            status=OutreachStatus.sent,
            external_id=str(canonical_id),
            draft_id=draft.id,
            metadata={"profile_url": person.linkedin_url,
                      "heyreach_campaign_id": canonical_id,
                      "followup_attached": bool(followup)},
        ),
        source_run_id=ctx.run_id,
    )
    ctx.result.records_inserted += 1
    # keep the logical campaign row pointing at the canonical HeyReach campaign
    ctx.db.client.table("campaigns").update(
        {"heyreach_campaign_id": str(canonical_id)}
    ).eq("id", str(draft.campaign_id)).execute()

    ctx.logger.info(
        "dispatched", person=person.full_name, heyreach_campaign=canonical_id,
        attempt=str(attempt.id), followup_attached=bool(followup),
    )
    return ctx.result.build(
        dispatched=True,
        heyreach_campaign_id=canonical_id,
        attempt_id=str(attempt.id),
        followup_attached=bool(followup),
    )
