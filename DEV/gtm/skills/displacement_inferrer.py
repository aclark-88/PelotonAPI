"""displacement_inferrer — incumbent system inference for one fund.

Web path: for each vendor in configs/vendors.yaml, searches the fund name +
vendor keywords (job posts surface most reliably; Google-cached LinkedIn posts
count — no LinkedIn API needed). Confidence accumulates per distinct hit,
with bonuses for incumbency phrasing ("experience with Geneva") and hits on
careers/job-board domains.

Vendors at/above min confidence are written to funds.known_incumbent_pms
(merged) with per-vendor detail in funds.metadata.incumbent_vendors, and a
displacement_inferred_job_post signal fires per vendor with the Clarion
displacement story from clarion_coverage.yaml in the payload — so outreach
drafting downstream can name the exact capability that replaces the incumbent.

ADV path (displacement_inferred_adv): requires the SEC private-funds FOIA file
(Schedule D 7.B service providers), which is not in the monthly firm roster.
Disabled via config until that file lands in data/adv/; the skill reports
adv_path_available=false rather than pretending.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from gtm.models.common import Urgency
from gtm.models.signals import SignalIn
from gtm.skills._shared import dedupe
from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.sources import SourceUnavailable

SKILL_NAME = "displacement_inferrer"


def _vendor_list(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for category, vendors in (cfg.get("vendors") or {}).items():
        for vendor in vendors:
            out.append({**vendor, "category": category})
    return out


def score_vendor_hits(
    hits: list[dict[str, Any]], confidence_cfg: dict[str, Any], phrases: list[str],
    job_domains: list[str],
) -> float:
    """Pure confidence model over search hits for one vendor."""
    confidence = 0.0
    for hit in hits:
        confidence += float(confidence_cfg.get("per_hit", 0.25))
        text = hit.get("text", "").lower()
        if any(p.lower() in text for p in phrases):
            confidence += float(confidence_cfg.get("incumbency_phrase_bonus", 0.25))
        url = hit.get("url", "").lower()
        if any(d in url for d in job_domains) or "career" in url or "job" in url:
            confidence += float(confidence_cfg.get("careers_domain_bonus", 0.2))
    return min(confidence, 1.0)


def run(ctx: SkillContext, fund_id: str) -> SkillResult:
    fund = ctx.db.funds.get(UUID(str(fund_id)))
    if fund is None:
        ctx.result.error("resolve", f"fund {fund_id} not found")
        return ctx.result.build()
    ctx.result.records_processed = 1

    try:
        web = ctx.sources.require("web")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build()

    cfg = ctx.config
    clarion_stories: dict[str, str] = (ctx.config.get("clarion") or {}).get(
        "vendor_displacement", {}
    )
    phrases: list[str] = cfg.get("incumbency_phrases", [])
    job_domains = ["linkedin.com/jobs", "greenhouse.io", "lever.co", "workable.com"]
    fund_token = (fund.common_name or fund.legal_name).replace(" LP", "").replace(" LLC", "").strip()
    inferred: list[dict[str, Any]] = []

    for vendor in _vendor_list(cfg):
        hits: list[dict[str, Any]] = []
        for keyword in vendor.get("keywords", [vendor["name"]]):
            query = f'"{fund_token}" "{keyword}"'
            try:
                results = web.search(query, max_results=int(cfg.get("max_results_per_query", 5)))
            except Exception as exc:
                ctx.result.error("search", exc, vendor=vendor["name"])
                continue
            for hit in results:
                text = f"{hit.title}. {hit.content}"
                if fund_token.lower() not in text.lower():
                    continue
                hits.append({"url": hit.url, "text": text})

        if not hits:
            continue
        confidence = score_vendor_hits(hits, cfg.get("confidence", {}), phrases, job_domains)
        if confidence < float(cfg.get("min_confidence_to_record", 0.3)):
            ctx.logger.info("vendor_below_threshold", vendor=vendor["name"], confidence=confidence)
            continue

        inferred.append(
            {
                "vendor": vendor["name"],
                "category": vendor["category"],
                "confidence": round(confidence, 2),
                "evidence_urls": [h["url"] for h in hits][:5],
            }
        )

        if ctx.dry_run:
            continue
        defaults = ctx.db.signals.type_defaults("displacement_inferred_job_post")
        urgency = Urgency(defaults["default_urgency"])
        if fund.tier in (1, 2):  # signal_weights: tier 1-2 jumps the queue
            urgency = Urgency.this_week
        signal = ctx.db.signals.record_signal(
            SignalIn(
                signal_type="displacement_inferred_job_post",
                source="web_search",
                source_record_id=dedupe.web_finding_record_id(
                    f"displacement:{fund.id}:{vendor['name'].lower()}", hits[0]["url"]
                ),
                observed_at=datetime.now(timezone.utc),
                fund_id=fund.id,
                urgency=urgency,
                payload={
                    "vendor": vendor["name"],
                    "category": vendor["category"],
                    "confidence": round(confidence, 2),
                    "evidence_urls": [h["url"] for h in hits][:5],
                    "clarion_displacement": clarion_stories.get(
                        vendor["name"], "unified PMS + managed middle office"
                    ),
                },
            ),
            source_run_id=ctx.run_id,
        )
        ctx.result.emit(signal.id)

    if inferred and not ctx.dry_run:
        vendors_found = [v["vendor"] for v in inferred]
        merged = sorted(set(fund.known_incumbent_pms) | set(vendors_found))
        ctx.db.funds.client.table("funds").update(
            {"known_incumbent_pms": merged}
        ).eq("id", str(fund.id)).execute()
        ctx.db.funds.update_metadata(fund.id, {"incumbent_vendors": inferred})
        ctx.result.records_updated = 1

    return ctx.result.build(
        inferred_vendors=inferred,
        adv_path_available=bool(cfg.get("adv_path_enabled", False)),
    )
