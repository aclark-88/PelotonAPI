"""linkedin_signal_reader — PLACEHOLDER built on web-search proxies.

No LinkedIn API is available in this environment. This skill currently:
  1. surfaces public LinkedIn posts via `site:linkedin.com "<fund>"` search
  2. counts publicly indexed job postings mentioning tech/ops/risk terms as a
     proxy for hiring velocity
  3. returns metadata.available=false when neither yields anything useful

It emits hiring_velocity_high when >= threshold distinct tech/ops/risk
postings appear inside the window (config: hiring_velocity_threshold).

UPGRADE PATH (documented, not built): a LinkedIn Sales Navigator / Recruiter
API key replaces both proxies — same run() signature, same signal contract,
just a real `linkedin` source in the SourceBundle instead of `web`. Nothing
downstream changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from gtm.models.common import Urgency
from gtm.models.signals import SignalIn
from gtm.skills._shared import dedupe
from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.sources import SourceUnavailable

SKILL_NAME = "linkedin_signal_reader"


def run(ctx: SkillContext, fund_id: str, lookback_days: int | None = None) -> SkillResult:
    fund = ctx.db.funds.get(UUID(str(fund_id)))
    if fund is None:
        ctx.result.error("resolve", f"fund {fund_id} not found")
        return ctx.result.build()
    ctx.result.records_processed = 1

    try:
        web = ctx.sources.require("web")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build(available=False)

    cfg = ctx.config
    lookback = int(lookback_days or cfg.get("lookback_days", 30))
    fund_token = (fund.common_name or fund.legal_name).replace(" LP", "").replace(" LLC", "").strip()
    role_terms: list[str] = cfg.get("hiring_role_terms", [])
    job_domains: list[str] = cfg.get("job_board_domains", ["linkedin.com"])

    # public LinkedIn posts
    try:
        posts = web.search(
            f'site:linkedin.com "{fund_token}"',
            max_results=int(cfg.get("max_results_per_query", 8)),
            days=lookback,
        )
    except Exception as exc:
        ctx.result.error("posts_search", exc)
        posts = []

    # job postings proxy
    job_hits: list[dict] = []
    try:
        results = web.search(
            f'"{fund_token}" hiring (operations OR risk OR technology OR engineer)',
            max_results=int(cfg.get("max_results_per_query", 8)),
            days=lookback,
        )
        for hit in results:
            text = f"{hit.title}. {hit.content}".lower()
            if fund_token.lower() not in text:
                continue
            matched_terms = [t for t in role_terms if t.lower() in text]
            on_job_board = any(d in hit.url.lower() for d in job_domains)
            if matched_terms and (on_job_board or "hiring" in text or "join" in text):
                job_hits.append({"url": hit.url, "terms": matched_terms})
    except Exception as exc:
        ctx.result.error("jobs_search", exc)

    distinct_postings = {h["url"] for h in job_hits}
    if not posts and not job_hits:
        ctx.logger.info("nothing_indexed", fund=fund.legal_name)
        return ctx.result.build(available=False)

    threshold = int(cfg.get("hiring_velocity_threshold", 3))
    if len(distinct_postings) >= threshold and not ctx.dry_run:
        defaults = ctx.db.signals.type_defaults("hiring_velocity_high")
        signal = ctx.db.signals.record_signal(
            SignalIn(
                signal_type="hiring_velocity_high",
                source="web_search",
                source_record_id=dedupe.web_finding_record_id(
                    f"hiring:{fund.id}", "|".join(sorted(distinct_postings))
                ),
                observed_at=datetime.now(timezone.utc),
                fund_id=fund.id,
                urgency=Urgency(defaults["default_urgency"]),
                payload={
                    "posting_count": len(distinct_postings),
                    "window_days": lookback,
                    "postings": job_hits[:10],
                    "method": "web_search_proxy",
                },
            ),
            source_run_id=ctx.run_id,
        )
        ctx.result.emit(signal.id)

    return ctx.result.build(
        available=True,
        public_posts=len(posts),
        job_postings=len(distinct_postings),
    )
