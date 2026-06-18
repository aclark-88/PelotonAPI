"""spinout_watcher — web-search detection of mothership spinouts.

For each mothership in configs/motherships.yaml, runs the configured query
patterns through web search, filters hits to those with real spinout context,
heuristically extracts a candidate fund name and founder, then:
  - upserts the candidate fund (parent_fund_id -> mothership when the
    mothership exists in funds), founder as person
  - emits spinout_detected (urgency immediate per signal_weights)
  - cross-references the candidate against funds already discovered by
    form_d_sweep (a Form D match lands in the payload as confirmation)

Dedupe is structural: one signal per (mothership, canonical URL) via
web_finding_record_id — the same spinout reported by five outlets produces
five URL-keyed signals only if all five pass filters, but the FUND is
upserted once (name match), and re-running the skill is a no-op.

Heuristic extraction is deliberately conservative: a fund row is only created
when a name candidate is found; otherwise the signal still fires with
fund_id=null (the schema allows signals to precede fund creation) and lands
in the human review queue.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from gtm.models.common import Urgency
from gtm.models.funds import FundIn
from gtm.models.people import PersonIn
from gtm.models.signals import SignalIn
from gtm.skills._shared import dedupe
from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.sources import SourceUnavailable

SKILL_NAME = "spinout_watcher"

# digits allowed mid-word: Point72, 26N-style names are common in this space
_CAP_PHRASE = r"([A-Z][A-Za-z0-9&'\-]+(?: [A-Z][A-Za-z0-9&'\-]+){0,3})"


def extract_fund_candidate(text: str, suffixes: list[str]) -> str | None:
    """First capitalized phrase ending in a fund-name suffix."""
    for suffix in sorted(suffixes, key=len, reverse=True):
        match = re.search(_CAP_PHRASE + rf" {re.escape(suffix)}\b", text)
        if match:
            return f"{match.group(1)} {suffix}".strip()
    return None


def extract_founder(text: str) -> str | None:
    """Names in 'X, a former ...' / 'former ... X is launching' shapes."""
    patterns = [
        rf"{_CAP_PHRASE},? (?:a |the )?former",
        rf"former [A-Za-z ]{{3,40}}? {_CAP_PHRASE} (?:is |will )?launch",
        rf"led by {_CAP_PHRASE}",
        rf"founded by {_CAP_PHRASE}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            name = match.group(1).strip()
            if 2 <= len(name.split()) <= 3:
                return name
    return None


def _has_context(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def run(ctx: SkillContext, lookback_days: int | None = None) -> SkillResult:
    cfg = ctx.config
    lookback = int(lookback_days or cfg.get("lookback_days", 7))
    try:
        web = ctx.sources.require("web")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build()

    motherships: list[dict[str, Any]] = cfg.get("motherships", [])
    patterns: list[str] = cfg.get("search_patterns", ['"{alias}" spinout hedge fund'])
    suffixes: list[str] = cfg.get("fund_name_suffixes", ["Capital", "Partners", "Management"])
    context_terms: list[str] = cfg.get("required_context_terms", ["spinout"])
    min_score = float(cfg.get("min_relevance_score", 0.4))
    seen_urls: set[str] = set()
    candidates_found: list[str] = []

    for mothership in motherships:
        ms_name = mothership["name"]
        ms_fund = (ctx.db.funds.search_by_name_fuzzy(ms_name, limit=1) or [None])[0]

        for alias in mothership.get("aliases", [ms_name]):
            query = patterns[0].format(alias=alias)
            try:
                results = web.search(
                    query,
                    max_results=int(cfg.get("max_results_per_query", 6)),
                    days=lookback,
                )
            except Exception as exc:
                ctx.result.error("search", exc, mothership=ms_name, alias=alias)
                continue

            for hit in results:
                ctx.result.records_processed += 1
                url = hit.url.strip().lower()
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                text = f"{hit.title}. {hit.content}"
                if hit.score < min_score or not _has_context(text, context_terms):
                    continue
                if alias.lower() not in text.lower():
                    continue

                fund_name = extract_fund_candidate(text, suffixes)
                founder = extract_founder(text)

                if ctx.dry_run:
                    ctx.logger.info("dry_run_spinout", mothership=ms_name, fund=fund_name, url=hit.url)
                    continue

                try:
                    fund_id = None
                    form_d_match = None
                    if fund_name and fund_name.lower() != ms_name.lower():
                        existing = ctx.db.funds.search_by_name_fuzzy(fund_name, limit=1)
                        if existing:
                            fund = existing[0]
                            # already known (often via form_d_sweep) -> confirmation
                            form_d_match = fund.cik
                            ctx.result.records_updated += 1
                        else:
                            fund = ctx.db.funds.upsert_fund(
                                FundIn(
                                    legal_name=fund_name,
                                    is_emerging_manager=True,
                                    parent_fund_id=ms_fund.id if ms_fund else None,
                                    metadata={"spinout_of": ms_name, "discovered_via": hit.url},
                                ),
                                source_run_id=ctx.run_id,
                            )
                            ctx.result.records_inserted += 1
                        fund_id = fund.id

                    person_id = None
                    if founder and fund_id:
                        person = ctx.db.people.upsert_person(
                            PersonIn(
                                full_name=founder,
                                current_fund_id=fund_id,
                                current_role="Founder",
                                metadata={"former_firm": ms_name, "discovered_via": hit.url},
                            ),
                            source_run_id=ctx.run_id,
                        )
                        person_id = person.id

                    signal = ctx.db.signals.record_signal(
                        SignalIn(
                            signal_type="spinout_detected",
                            source="web_search",
                            source_record_id=dedupe.web_finding_record_id(
                                f"spinout:{ms_name.lower()}", hit.url
                            ),
                            observed_at=datetime.now(timezone.utc),
                            fund_id=fund_id,
                            person_id=person_id,
                            urgency=Urgency.immediate,  # signal_weights: spinouts always immediate
                            payload={
                                "mothership": ms_name,
                                "candidate_fund": fund_name,
                                "founder": founder,
                                "title": hit.title,
                                "url": hit.url,
                                "snippet": hit.content[:500],
                                "form_d_cross_reference": form_d_match,
                                "needs_verification": fund_name is None or founder is None,
                            },
                        ),
                        source_run_id=ctx.run_id,
                    )
                    ctx.result.emit(signal.id)
                    if fund_name:
                        candidates_found.append(fund_name)
                except Exception as exc:
                    ctx.result.error("candidate", exc, mothership=ms_name, url=hit.url)

    return ctx.result.build(candidates=candidates_found, urls_seen=len(seen_urls))
