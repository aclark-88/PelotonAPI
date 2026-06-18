"""people_move_detector — buying-committee moves into TAM funds.

Two ingestion paths:
  1. Apollo: for each TAM fund (tier 1-3 or fit >= threshold, capped per run),
     search current holders of target titles at the fund's domain. If Apollo
     shows a person in a target seat and our DB has them elsewhere (or not at
     all with a different prior employer), that's an observed move ->
     fn_observe_job_change (atomic: employment_history + people + new_role
     signal). The per-role signal type (new_coo, new_head_risk, ...) is
     emitted alongside, keyed to the same deterministic record id.
  2. Web search: role-appointment news queries; a hit that resolves to a known
     fund gets person resolution via Apollo enrich, then the same flow.

Champion check: after each observed move, the person is looked up in HubSpot
(email first, then name). A match = prior relationship -> urgency forced to
immediate and champion_relocation=true on the signal (signal_weights.yaml).
HubSpot being unconfigured degrades gracefully: moves still record, the
champion check is skipped with a logged warning.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from gtm.models.common import RoleFunction, Seniority, Urgency
from gtm.models.people import PersonIn
from gtm.models.signals import SignalIn
from gtm.skills._shared.apollo import ApolloPerson
from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.sources import SourceUnavailable

SKILL_NAME = "people_move_detector"


def _title_matches(title: str | None, role_cfg: dict[str, Any]) -> bool:
    if not title:
        return False
    lowered = title.lower()
    return any(t.lower() in lowered for t in role_cfg.get("titles", []))


def _observe(
    ctx: SkillContext,
    person_id,
    fund,
    apollo_person: ApolloPerson,
    role_key: str,
    role_cfg: dict[str, Any],
) -> None:
    """Run the atomic job-change flow + role signal + champion check."""
    signal = ctx.db.people.observe_job_change(
        person_id=person_id,
        new_fund_id=fund.id,
        new_role=apollo_person.title or role_cfg["titles"][0],
        observed_at=datetime.now(timezone.utc),
        function=RoleFunction(role_cfg.get("function", "unknown")),
        seniority=Seniority(role_cfg.get("seniority", "unknown")),
        source="apollo",
        source_run_id=ctx.run_id,
    )
    ctx.result.emit(signal.id)

    # role-specific signal (new_coo, new_head_risk, ...) for campaign routing
    role_signal_type = role_cfg.get("signal_type")
    if role_signal_type:
        defaults = ctx.db.signals.type_defaults(role_signal_type)
        role_signal = ctx.db.signals.record_signal(
            SignalIn(
                signal_type=role_signal_type,
                source="apollo",
                source_record_id=f"{role_key}:{person_id}:{fund.id}",
                observed_at=datetime.now(timezone.utc),
                fund_id=fund.id,
                person_id=person_id,
                urgency=Urgency(defaults["default_urgency"]),
                payload={
                    "person": apollo_person.name,
                    "title": apollo_person.title,
                    "fund": fund.legal_name,
                    "linkedin_url": apollo_person.linkedin_url,
                },
            ),
            source_run_id=ctx.run_id,
        )
        ctx.result.emit(role_signal.id)

    # ── champion relocation check (HubSpot) ─────────────────────────────────
    try:
        hubspot = ctx.sources.require("hubspot")
        contact = hubspot.find_contact(email=apollo_person.email, name=apollo_person.name)
        if contact:
            ctx.db.signals.update_urgency(
                signal.id,
                Urgency.immediate,
                metadata_patch={
                    "champion_relocation": True,
                    "hubspot_contact_id": contact.get("id"),
                },
            )
            ctx.logger.info(
                "champion_relocation",
                person=apollo_person.name,
                fund=fund.legal_name,
                hubspot_id=contact.get("id"),
            )
    except SourceUnavailable:
        ctx.logger.warning("hubspot_unavailable_champion_check_skipped")
    except Exception as exc:
        ctx.result.error("champion_check", exc, person=apollo_person.name)


def run(ctx: SkillContext, lookback_hours: int | None = None) -> SkillResult:
    cfg = ctx.config
    try:
        apollo = ctx.sources.require("apollo")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build()

    target_functions: dict[str, dict[str, Any]] = cfg.get("target_functions", {})
    all_titles = [t for rc in target_functions.values() for t in rc.get("titles", [])]
    moves: list[dict[str, Any]] = []

    # ── Path 1: Apollo over TAM funds ────────────────────────────────────────
    tam = ctx.db.funds.list_tam(
        min_fit_score=int(cfg.get("tam_min_fit_score", 60)),
        limit=int(cfg.get("max_funds_per_run", 25)),
    )
    ctx.logger.info("tam_funds", count=len(tam))

    for fund in tam:
        try:
            people = apollo.search_people(domain=fund.primary_domain, titles=all_titles)
        except Exception as exc:
            ctx.result.error("apollo_search", exc, fund=fund.legal_name)
            continue

        for ap in people:
            ctx.result.records_processed += 1
            role_key, role_cfg = next(
                (
                    (k, rc)
                    for k, rc in target_functions.items()
                    if _title_matches(ap.title, rc)
                ),
                (None, None),
            )
            if role_key is None:
                continue

            # api_search returns first-name-only previews; resolve full
            # identity via enrich (credit spend) before touching the DB.
            if " " not in ap.name.strip():
                if ap.linkedin_url:
                    try:
                        enriched = apollo.enrich_person(linkedin_url=ap.linkedin_url)
                        if enriched and " " in enriched.name.strip():
                            ap = enriched
                        else:
                            raise ValueError("enrich returned no full name")
                    except Exception as exc:
                        ctx.logger.info("preview_unresolved", name=ap.name, fund=fund.legal_name, error=str(exc))
                        continue
                else:
                    ctx.logger.info("preview_skipped_no_linkedin", name=ap.name, fund=fund.legal_name)
                    continue

            existing = ctx.db.people.upsert_person(
                PersonIn(
                    full_name=ap.name,
                    email=ap.email,
                    linkedin_url=ap.linkedin_url,
                    apollo_id=ap.apollo_id,
                    metadata={"apollo_seniority": ap.seniority},
                ),
                source_run_id=ctx.run_id,
            )

            if existing.current_fund_id == fund.id:
                continue  # already known in this seat — not a move
            if ctx.dry_run:
                ctx.logger.info("dry_run_move", person=ap.name, fund=fund.legal_name)
                continue
            try:
                _observe(ctx, existing.id, fund, ap, role_key, role_cfg)
                moves.append({"person": ap.name, "fund": fund.legal_name, "role": role_key})
                ctx.result.records_updated += 1
            except Exception as exc:
                ctx.result.error("observe", exc, person=ap.name, fund=fund.legal_name)

    # ── Path 2: web-search appointment news ──────────────────────────────────
    web_hits = 0
    try:
        web = ctx.sources.require("web")
        for role_key, role_cfg in target_functions.items():
            for template in cfg.get("web_queries", []):
                query = template.format(role=role_cfg["titles"][0])
                try:
                    results = web.search(
                        query,
                        max_results=int(cfg.get("web_max_results_per_query", 5)),
                        days=int(cfg.get("web_lookback_days", 2)),
                    )
                except Exception as exc:
                    ctx.result.error("web_search", exc, role=role_key)
                    continue
                for hit in results:
                    ctx.result.records_processed += 1
                    web_hits += 1
                    matched_fund = None
                    for fund in tam:
                        token = (fund.common_name or fund.legal_name).split(" LP")[0].split(" LLC")[0]
                        if len(token) > 4 and token.lower() in f"{hit.title} {hit.content}".lower():
                            matched_fund = fund
                            break
                    if matched_fund is None or ctx.dry_run:
                        continue
                    try:
                        enriched = apollo.enrich_person(
                            domain=matched_fund.primary_domain,
                            name=None,
                            linkedin_url=None,
                        )
                    except Exception:
                        enriched = None
                    if enriched is None:
                        ctx.logger.info(
                            "web_hit_unresolved", url=hit.url, fund=matched_fund.legal_name
                        )
                        continue
                    person = ctx.db.people.upsert_person(
                        PersonIn(
                            full_name=enriched.name,
                            email=enriched.email,
                            linkedin_url=enriched.linkedin_url,
                            apollo_id=enriched.apollo_id,
                        ),
                        source_run_id=ctx.run_id,
                    )
                    if person.current_fund_id != matched_fund.id:
                        try:
                            _observe(ctx, person.id, matched_fund, enriched, role_key, role_cfg)
                            moves.append(
                                {"person": enriched.name, "fund": matched_fund.legal_name, "role": role_key}
                            )
                        except Exception as exc:
                            ctx.result.error("observe_web", exc, person=enriched.name)
    except SourceUnavailable:
        ctx.logger.warning("web_search_unavailable_path2_skipped")

    return ctx.result.build(moves=moves, web_hits=web_hits)
