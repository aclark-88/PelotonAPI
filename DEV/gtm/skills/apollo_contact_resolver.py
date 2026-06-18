"""apollo_contact_resolver — fill the buying committee for one fund.

For each configured role, searches Apollo at the fund's domain, resolves
full identity (api_search previews are first-name-only; enrich unlocks the
rest), and upserts people with confidence in metadata. LinkedIn-first: a
contact with no resolvable linkedin_url is recorded but flagged, since this
campaign is LinkedIn-only via HeyReach.

Unfilled roles emit contact_gap signals (deduped per fund+role) for the
human research queue. Manually verified contacts are never overwritten —
enforced at the repository layer (metadata.manually_verified).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from gtm.models.common import RoleFunction, Seniority, Urgency
from gtm.models.people import PersonIn
from gtm.models.signals import SignalIn
from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.sources import SourceUnavailable

SKILL_NAME = "apollo_contact_resolver"


def _title_matches(title: str | None, role_cfg: dict[str, Any]) -> bool:
    if not title:
        return False
    lowered = title.lower()
    return any(t.lower() in lowered for t in role_cfg.get("titles", []))


def run(ctx: SkillContext, fund_id: str, roles: list[str] | None = None) -> SkillResult:
    fund = ctx.db.funds.get(UUID(str(fund_id)))
    if fund is None:
        ctx.result.error("resolve", f"fund {fund_id} not found")
        return ctx.result.build()
    # Launches often have no domain yet (no ADV, thin web). Apollo can search
    # by organization name as a fallback; domain stays preferred when known.
    org_name = None
    if not fund.primary_domain:
        org_name = (fund.common_name or fund.legal_name).replace(" LP", "").replace(" L.P.", "").replace(" LLC", "").strip()

    try:
        apollo = ctx.sources.require("apollo")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build()

    role_cfgs: dict[str, dict[str, Any]] = ctx.config.get("roles", {})
    wanted = {k: v for k, v in role_cfgs.items() if roles is None or k in roles}
    resolved: dict[str, list[str]] = {}
    gaps: list[str] = []

    for role_key, role_cfg in wanted.items():
        try:
            candidates = apollo.search_people(
                domain=str(fund.primary_domain) if fund.primary_domain else None,
                org_name=org_name,
                titles=role_cfg.get("titles", []),
                per_page=int(ctx.config.get("per_role_limit", 3)),
            )
        except Exception as exc:
            ctx.result.error("apollo_search", exc, role=role_key)
            continue

        matches = [c for c in candidates if _title_matches(c.title, role_cfg)]
        if not matches:
            gaps.append(role_key)
            continue

        for candidate in matches:
            ctx.result.records_processed += 1
            # resolve first-name-only previews before touching the DB.
            # Enrichment spend is pre-approved (standing permission).
            if " " not in candidate.name.strip() or not candidate.linkedin_url:
                try:
                    enriched = apollo.enrich_person(
                        person_id=candidate.apollo_id or None,
                        linkedin_url=candidate.linkedin_url,
                    )
                except Exception as exc:
                    ctx.result.error("enrich", exc, role=role_key)
                    continue
                if not enriched or " " not in enriched.name.strip():
                    ctx.logger.info("enrich_unresolved", role=role_key, preview=candidate.name)
                    continue
                candidate = enriched

            if ctx.dry_run:
                ctx.logger.info("dry_run_contact", name=candidate.name, role=role_key)
                continue
            person = ctx.db.people.upsert_person(
                PersonIn(
                    full_name=candidate.name,
                    email=candidate.email,
                    linkedin_url=candidate.linkedin_url,
                    apollo_id=candidate.apollo_id,
                    current_fund_id=fund.id,
                    current_role=candidate.title,
                    current_role_function=RoleFunction(role_cfg.get("function", "unknown")),
                    current_role_seniority=Seniority(role_cfg.get("seniority", "unknown")),
                    metadata={
                        "apollo_confidence": "verified" if candidate.email else "title_match",
                        "resolved_role": role_key,
                        "linkedin_resolvable": bool(candidate.linkedin_url),
                    },
                ),
                source_run_id=ctx.run_id,
            )
            resolved.setdefault(role_key, []).append(str(person.id))
            ctx.result.records_inserted += 1
            if not candidate.linkedin_url:
                ctx.logger.warning("no_linkedin_url", person=candidate.name, role=role_key)

    # contact_gap signals for the human queue (deduped per fund+role)
    for role_key in gaps:
        if ctx.dry_run:
            continue
        defaults = ctx.db.signals.type_defaults("contact_gap")
        signal = ctx.db.signals.record_signal(
            SignalIn(
                signal_type="contact_gap",
                source="apollo",
                source_record_id=f"gap:{fund.id}:{role_key}",
                observed_at=datetime.now(timezone.utc),
                fund_id=fund.id,
                urgency=Urgency(defaults["default_urgency"]),
                payload={"role": role_key, "fund": fund.legal_name,
                         "domain": str(fund.primary_domain)},
            ),
            source_run_id=ctx.run_id,
        )
        ctx.result.emit(signal.id)

    return ctx.result.build(resolved=resolved, gaps=gaps, fund_id=str(fund.id))
