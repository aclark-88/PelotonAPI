"""meeting_brief_generator — 1-page pre-meeting brief for a fund.

Inputs: fund_id + person_ids (primary path), or a HubSpot meeting_id (resolved
to attendees via associated contacts' emails). Assembles, deterministically:
fund overview, signals (last 180d), attendee bios with employment history and
prior outreach/replies, likely incumbent stack, three talking points keyed to
the strongest signals (config templates, capability claims trace to the
Clarion canon), two rule-selected objections with responses, and recent
closed-won deals from HubSpot (no local closed_won table — non-goal honored).

Output: gtm/briefs/<date>_<fund_slug>.md (+ Slack post when configured).
Deterministic by design — the orchestrating Claude can always enrich the
markdown afterwards; this skill guarantees the facts are assembled and filed.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.sources import SourceUnavailable

SKILL_NAME = "meeting_brief_generator"

REPO_ROOT = Path(__file__).resolve().parents[2]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def _fill(template: str, payload: dict[str, Any]) -> str:
    def sub(match: re.Match) -> str:
        return str(payload.get(match.group(1), f"<{match.group(1)}?>"))

    return re.sub(r"\{payload\.(\w+)\}", sub, template)


def _talking_points(ctx: SkillContext, signals: list) -> list[str]:
    templates: dict[str, str] = ctx.config.get("signal_talking_points", {})
    urgency_rank = {"immediate": 0, "this_week": 1, "this_month": 2, "archive": 3}
    ranked = sorted(
        (s for s in signals if s.signal_type in templates),
        key=lambda s: (urgency_rank.get(s.urgency.value, 9), -s.observed_at.timestamp()),
    )
    points, seen_types = [], set()
    for signal in ranked:
        if signal.signal_type in seen_types:
            continue
        seen_types.add(signal.signal_type)
        points.append(_fill(templates[signal.signal_type], signal.payload))
        if len(points) == 3:
            break
    return points


def _objections(ctx: SkillContext, fund) -> list[dict[str, str]]:
    bank: list[dict[str, Any]] = ctx.config.get("objections", [])
    flags = {
        "is_emerging_manager": bool(fund.is_emerging_manager),
        "has_incumbent": bool(fund.known_incumbent_pms),
        "always": True,
    }
    picked = []
    for entry in bank:
        if flags.get(entry.get("when", "always"), False):
            picked.append(entry)
        if len(picked) == 2:
            break
    return picked


def _resolve_attendees(ctx: SkillContext, person_ids, meeting_id) -> list:
    if person_ids:
        people = [ctx.db.people.get(UUID(str(p))) for p in person_ids]
        return [p for p in people if p is not None]
    if meeting_id:
        hubspot = ctx.sources.require("hubspot")
        meeting = hubspot.get_meeting(str(meeting_id))
        if not meeting:
            return []
        contact_ids = [
            str(r["id"])
            for assoc in (meeting.get("associations") or {}).get("contacts", {}).get("results", [])
            for r in [assoc]
        ]
        attendees = []
        for cid in contact_ids:
            contact = hubspot.get_contact(cid)
            email = (contact or {}).get("properties", {}).get("email")
            if not email:
                continue
            rows = (
                ctx.db.client.table("people").select("id").eq("email", email)
                .is_("deleted_at", "null").limit(1).execute()
            ).data
            if rows:
                attendees.append(ctx.db.people.get(UUID(rows[0]["id"])))
        return [a for a in attendees if a]
    return []


def run(
    ctx: SkillContext,
    fund_id: str | None = None,
    person_ids: list[str] | None = None,
    meeting_id: str | None = None,
) -> SkillResult:
    attendees = _resolve_attendees(ctx, person_ids, meeting_id)
    if fund_id is None and attendees and attendees[0].current_fund_id:
        fund_id = str(attendees[0].current_fund_id)
    fund = ctx.db.funds.get(UUID(str(fund_id))) if fund_id else None
    if fund is None:
        ctx.result.error("resolve", f"fund not resolved (fund_id={fund_id}, meeting_id={meeting_id})")
        return ctx.result.build()
    ctx.result.records_processed = 1

    lookback = int(ctx.config.get("signal_lookback_days", 180))
    since = datetime.now(timezone.utc) - timedelta(days=lookback)
    signals = [s for s in ctx.db.signals.list_for_fund(fund.id, limit=200) if s.observed_at >= since]

    # prior outreach + replies per attendee
    outreach: dict[str, list[dict[str, Any]]] = {}
    for person in attendees:
        attempts = (
            ctx.db.client.table("outreach_attempts").select("*")
            .eq("person_id", str(person.id)).is_("deleted_at", "null")
            .order("created_at", desc=True).limit(20).execute()
        ).data
        for attempt in attempts:
            replies = (
                ctx.db.client.table("replies").select("body, sentiment, intent, received_at")
                .eq("outreach_attempt_id", attempt["id"]).is_("deleted_at", "null").execute()
            ).data
            attempt["replies"] = replies
        outreach[str(person.id)] = attempts

    # closed-won proof from HubSpot (degrades without the source)
    won: list[dict[str, Any]] = []
    try:
        hubspot = ctx.sources.require("hubspot")
        won = hubspot.won_deals(limit=int(ctx.config.get("won_deals_count", 3)))
    except SourceUnavailable:
        ctx.logger.warning("hubspot_unavailable_no_won_deals")
    except Exception as exc:
        ctx.result.error("won_deals", exc)

    # ── assemble markdown ────────────────────────────────────────────────────
    today = datetime.now(timezone.utc).date().isoformat()
    lines: list[str] = [
        f"# Meeting brief — {fund.common_name or fund.legal_name}",
        f"*Generated {today} · fit {fund.fit_score or '?'} · tier {fund.tier or '?'} · "
        f"AUM band {fund.aum_band} · strategies: {', '.join(fund.strategies) or 'unknown'}*",
        "",
        "## Fund overview",
        f"- Legal name: {fund.legal_name}",
        f"- HQ: {fund.headquarters_city or '?'}, {fund.headquarters_country or '?'}"
        f" · emerging manager: {bool(fund.is_emerging_manager)}",
        f"- Prime brokers: {', '.join(fund.prime_brokers) or 'unknown'} · "
        f"administrator: {fund.administrator or 'unknown'}",
        "",
        f"## Signals (last {lookback}d)",
    ]
    if signals:
        lines.append("| When | Type | Urgency | Key detail |")
        lines.append("|---|---|---|---|")
        for s in signals[:12]:
            detail = s.payload.get("vendor") or s.payload.get("new_role") or \
                s.payload.get("issuer") or s.payload.get("candidate_fund") or ""
            lines.append(
                f"| {s.observed_at.date()} | {s.signal_type} | {s.urgency.value} | {detail} |"
            )
    else:
        lines.append("_No signals in window — confirm why this meeting exists._")

    lines += ["", "## Attendees"]
    for person in attendees:
        history = ctx.db.people.employment_history(person.id)
        past = "; ".join(
            f"{h.role or '?'} ({h.started_at or '?'} – {h.ended_at or 'now'})" for h in history[:4]
        )
        attempts = outreach.get(str(person.id), [])
        replied = [a for a in attempts if a.get("replies")]
        lines += [
            f"### {person.full_name} — {person.current_role or '?'}",
            f"- Buying committee: {person.is_buying_committee_member} · "
            f"function: {person.current_role_function.value} / {person.current_role_seniority.value}",
            f"- History: {past or 'none recorded'}",
            f"- Prior outreach: {len(attempts)} touch(es), {len(replied)} replied"
            + (f" — last reply sentiment: {replied[0]['replies'][0].get('sentiment')}" if replied else ""),
        ]
    if not attendees:
        lines.append("_No attendees resolved._")

    incumbents = fund.metadata.get("incumbent_vendors") or [
        {"vendor": v, "confidence": "known"} for v in fund.known_incumbent_pms
    ]
    lines += ["", "## Likely incumbent stack"]
    if incumbents:
        for vendor in incumbents:
            lines.append(f"- {vendor['vendor']} (confidence: {vendor.get('confidence', '?')})"
                         " — do not name first in the meeting")
    else:
        lines.append("_Unknown — displacement_inferrer has no evidence yet._")

    lines += ["", "## Talking points"]
    for i, point in enumerate(_talking_points(ctx, signals), 1):
        lines.append(f"{i}. {point}")
    lines += ["", "## Probable objections"]
    for entry in _objections(ctx, fund):
        lines += [f"- **\"{entry['objection']}\"**", f"  - {entry['response']}"]

    lines += ["", "## Recent closed-won (HubSpot)"]
    if won:
        for deal in won:
            props = deal.get("properties", {})
            lines.append(f"- {props.get('dealname', '?')} (closed {str(props.get('closedate', '?'))[:10]},"
                         f" amount {props.get('amount') or '?'})")
    else:
        lines.append("_None available._")

    content = "\n".join(lines) + "\n"
    out_dir = REPO_ROOT / str(ctx.config.get("output_dir", "gtm/briefs"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{today}_{_slug(fund.common_name or fund.legal_name)}.md"
    if not ctx.dry_run:
        path.write_text(content, encoding="utf-8")
        ctx.result.records_inserted = 1

    if ctx.config.get("post_to_slack", True) and not ctx.dry_run:
        try:
            slack = ctx.sources.require("slack")
            slack.post(f"Meeting brief ready: {fund.common_name or fund.legal_name} — {path.name}")
        except SourceUnavailable:
            ctx.logger.info("slack_not_configured_skipping_post")
        except Exception as exc:
            ctx.result.error("slack", exc)

    return ctx.result.build(
        brief_path=str(path),
        signal_count=len(signals),
        attendee_count=len(attendees),
        won_deals=len(won),
    )
