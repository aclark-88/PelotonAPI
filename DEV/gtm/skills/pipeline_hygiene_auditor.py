"""pipeline_hygiene_auditor — open-pipeline health sweep.

For every open HubSpot deal: last-activity staleness, next-step presence,
contacts-still-at-company (cross-referenced against our employment_history,
which people_move_detector keeps current), and signal freshness on the
matched fund (any signal in the last 90d).

Outputs a markdown report to gtm/briefs/hygiene_<date>.md, posts to Slack
when configured (Monday 7am scheduling lives in the orchestrator), and emits
one opp_at_risk signal per flagged deal per ISO week (deduped — a deal that
stays broken re-fires weekly, not per run).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from gtm.models.common import Urgency
from gtm.models.signals import SignalIn
from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.sources import SourceUnavailable

SKILL_NAME = "pipeline_hygiene_auditor"

REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _match_fund(ctx: SkillContext, company: dict[str, Any] | None):
    if not company:
        return None
    props = company.get("properties", {})
    domain, name = props.get("domain"), props.get("name")
    if domain:
        rows = (
            ctx.db.client.table("funds").select("id").eq("primary_domain", domain)
            .is_("deleted_at", "null").limit(1).execute()
        ).data
        if rows:
            return ctx.db.funds.get(UUID(rows[0]["id"]))
    if name:
        matches = ctx.db.funds.search_by_name_fuzzy(name, limit=1)
        if matches:
            return matches[0]
    return None


def audit_deal(
    ctx: SkillContext, hubspot, deal: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Pure-ish per-deal audit; returns the deal's flag dict."""
    props = deal.get("properties", {})
    flags: list[str] = []
    detail: dict[str, Any] = {}

    last_activity = _parse_ts(props.get("notes_last_updated")) or _parse_ts(
        props.get("hs_lastmodifieddate")
    )
    stale_days = int(ctx.config.get("stale_after_days", 14))
    if last_activity is None or (now - last_activity) > timedelta(days=stale_days):
        flags.append("stale")
        detail["last_activity"] = str(last_activity.date()) if last_activity else "never"

    if not (props.get("hs_next_step") or "").strip():
        flags.append("no_next_step")

    fund = None
    try:
        company_ids = hubspot.deal_associations(deal["id"], "companies")
        company = hubspot.get_company(company_ids[0]) if company_ids else None
        fund = _match_fund(ctx, company)
    except Exception as exc:
        ctx.result.error("associations", exc, deal=deal["id"])

    departed: list[str] = []
    try:
        for contact_id in hubspot.deal_associations(deal["id"], "contacts"):
            contact = hubspot.get_contact(contact_id)
            email = (contact or {}).get("properties", {}).get("email")
            if not email:
                continue
            rows = (
                ctx.db.client.table("people").select("id, full_name, current_fund_id")
                .eq("email", email).is_("deleted_at", "null").limit(1).execute()
            ).data
            if rows and fund and rows[0]["current_fund_id"] not in (str(fund.id), None):
                departed.append(rows[0]["full_name"])
    except Exception as exc:
        ctx.result.error("contacts_check", exc, deal=deal["id"])
    if departed:
        flags.append("contact_left")
        detail["departed"] = departed

    if fund is not None:
        decay_days = int(ctx.config.get("signal_decay_days", 90))
        fresh_since = now - timedelta(days=decay_days)
        recent = [
            s for s in ctx.db.signals.list_for_fund(fund.id, limit=50)
            if s.observed_at >= fresh_since
        ]
        if not recent:
            flags.append("signals_decayed")
    else:
        detail["fund_match"] = "none"

    return {
        "deal_id": deal["id"],
        "name": props.get("dealname", "?"),
        "stage": props.get("dealstage", "?"),
        "flags": flags,
        "detail": detail,
        "fund_id": str(fund.id) if fund else None,
    }


def run(ctx: SkillContext) -> SkillResult:
    try:
        hubspot = ctx.sources.require("hubspot")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build()

    now = datetime.now(timezone.utc)
    deals = hubspot.open_deals(limit=int(ctx.config.get("max_deals", 100)))
    ctx.logger.info("open_deals", count=len(deals))

    audits: list[dict[str, Any]] = []
    for deal in deals:
        ctx.result.records_processed += 1
        try:
            audits.append(audit_deal(ctx, hubspot, deal, now))
        except Exception as exc:
            ctx.result.error("audit", exc, deal=deal.get("id"))

    flagged = [a for a in audits if a["flags"]]

    # opp_at_risk per flagged deal per ISO week
    iso_week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    for audit in flagged:
        if ctx.dry_run:
            continue
        try:
            defaults = ctx.db.signals.type_defaults("opp_at_risk")
            signal = ctx.db.signals.record_signal(
                SignalIn(
                    signal_type="opp_at_risk",
                    source="hubspot",
                    source_record_id=f"opp:{audit['deal_id']}:{iso_week}",
                    observed_at=now,
                    fund_id=UUID(audit["fund_id"]) if audit["fund_id"] else None,
                    urgency=Urgency(defaults["default_urgency"]),
                    payload={k: v for k, v in audit.items() if k != "fund_id"},
                ),
                source_run_id=ctx.run_id,
            )
            ctx.result.emit(signal.id)
        except Exception as exc:
            ctx.result.error("signal", exc, deal=audit["deal_id"])

    # ── markdown report ──────────────────────────────────────────────────────
    today = now.date().isoformat()
    lines = [
        f"# Pipeline hygiene — {today}",
        f"*{len(deals)} open deals · {len(flagged)} flagged*",
        "",
    ]
    sections = {
        "stale": "## Stale (no recent activity)",
        "no_next_step": "## Missing next step",
        "contact_left": "## Contact left the company",
        "signals_decayed": "## Signals decayed (90d quiet)",
    }
    for flag, header in sections.items():
        hits = [a for a in flagged if flag in a["flags"]]
        if not hits:
            continue
        lines.append(header)
        for audit in hits:
            extra = ""
            if flag == "stale":
                extra = f" (last activity: {audit['detail'].get('last_activity', '?')})"
            if flag == "contact_left":
                extra = f" (departed: {', '.join(audit['detail'].get('departed', []))})"
            lines.append(f"- {audit['name']} [{audit['stage']}]{extra}")
        lines.append("")
    if not flagged:
        lines.append("All clear — every open deal has activity, a next step, present contacts, and fresh signals.")

    content = "\n".join(lines) + "\n"
    out_dir = REPO_ROOT / str(ctx.config.get("output_dir", "gtm/briefs"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"hygiene_{today}.md"
    if not ctx.dry_run:
        path.write_text(content, encoding="utf-8")
        ctx.result.records_inserted = 1

    if ctx.config.get("post_to_slack", True) and not ctx.dry_run:
        try:
            slack = ctx.sources.require("slack")
            slack.post(
                f"Pipeline hygiene {today}: {len(flagged)}/{len(deals)} deals flagged — {path.name}"
            )
        except SourceUnavailable:
            ctx.logger.info("slack_not_configured_skipping_post")
        except Exception as exc:
            ctx.result.error("slack", exc)

    return ctx.result.build(
        report_path=str(path),
        open_deals=len(deals),
        flagged=[{k: a[k] for k in ("name", "flags")} for a in flagged],
    )
