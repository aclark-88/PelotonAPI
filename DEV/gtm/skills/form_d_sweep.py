"""form_d_sweep — daily Form D discovery.

Pulls recent Form D filings (pooled investment vehicles), upserts fund
records, and emits:
  - capital_raise_form_d   for every qualifying filing (keyed to accession)
  - new_fund_launch        when it is the issuer's first-ever original Form D
                           (keyed to the CIK, so amendments can never re-fire)

Idempotent: signals dedupe on (source, source_record_id, signal_type); fund
upserts match CIK first. Re-running a sweep is always safe.
"""

from __future__ import annotations

from typing import Any

from gtm.models.common import Urgency
from gtm.models.funds import FundIn
from gtm.models.signals import SignalIn
from gtm.skills._shared import dedupe
from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.sources import FormDRecord, SourceUnavailable

SKILL_NAME = "form_d_sweep"


def _is_candidate(rec: FormDRecord, cfg: dict[str, Any]) -> tuple[bool, str]:
    """(qualifies, reason_if_not)."""
    if cfg.get("require_pooled_investment", True):
        if (rec.industry_group or "").strip().lower() != "pooled investment fund":
            return False, "not_pooled_investment"
    if rec.fund_type and rec.fund_type in set(cfg.get("exclude_fund_types", [])):
        return False, f"excluded_type:{rec.fund_type}"
    name = rec.issuer_name.lower()
    for term in cfg.get("negative_name_terms", []):
        if term.lower() in name:
            return False, f"negative_term:{term}"
    min_offering = cfg.get("min_offering_usd")
    if (
        min_offering
        and rec.total_offering_usd is not None
        and rec.total_offering_usd < float(min_offering)
    ):
        return False, "below_min_offering"
    return True, ""


def run(
    ctx: SkillContext,
    lookback_days: int | None = None,
    min_offering_usd: float | None = None,
) -> SkillResult:
    cfg = dict(ctx.config)
    if lookback_days is not None:
        cfg["lookback_days"] = lookback_days
    if min_offering_usd is not None:
        cfg["min_offering_usd"] = min_offering_usd

    try:
        edgar = ctx.sources.require("edgar")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build()

    records = edgar.recent_form_d(
        int(cfg.get("lookback_days", 1)), max_filings=int(cfg.get("max_filings", 200))
    )
    ctx.logger.info("form_d_fetched", count=len(records))

    fund_ids: list[str] = []
    skipped: dict[str, int] = {}

    for rec in records:
        ctx.result.records_processed += 1
        qualifies, reason = _is_candidate(rec, cfg)
        if not qualifies:
            key = reason.split(":")[0]
            skipped[key] = skipped.get(key, 0) + 1
            continue

        try:
            if ctx.dry_run:
                ctx.logger.info("dry_run_fund", issuer=rec.issuer_name, cik=rec.cik)
                continue

            existing = ctx.db.funds.find_by_cik(rec.cik)
            fund = ctx.db.funds.upsert_fund(
                FundIn(
                    legal_name=rec.issuer_name,
                    cik=rec.cik,
                    is_emerging_manager=True if existing is None else None,
                    metadata={"form_d_latest_accession": rec.accession},
                ),
                source_run_id=ctx.run_id,
            )
            if existing is None:
                ctx.result.records_inserted += 1
            else:
                ctx.result.records_updated += 1
            fund_ids.append(str(fund.id))

            raise_defaults = ctx.db.signals.type_defaults("capital_raise_form_d")
            signal = ctx.db.signals.record_signal(
                SignalIn(
                    signal_type="capital_raise_form_d",
                    source="edgar_tools",
                    source_record_id=dedupe.form_d_record_id(rec.accession),
                    observed_at=rec.filed_at,
                    fund_id=fund.id,
                    urgency=Urgency(raise_defaults["default_urgency"]),
                    payload={
                        "accession": rec.accession,
                        "offering_usd": rec.total_offering_usd,
                        "sold_usd": rec.total_sold_usd,
                        "investor_count": rec.investor_count,
                        "declared_fund_type": rec.fund_type,
                        "is_amendment": rec.is_amendment,
                        "related_persons": rec.related_persons,
                    },
                ),
                source_run_id=ctx.run_id,
            )
            ctx.result.emit(signal.id)

            if not rec.is_amendment and edgar.form_d_history_count(rec.cik) <= 1:
                launch_defaults = ctx.db.signals.type_defaults("new_fund_launch")
                launch = ctx.db.signals.record_signal(
                    SignalIn(
                        signal_type="new_fund_launch",
                        source="edgar_tools",
                        source_record_id=dedupe.form_d_launch_record_id(rec.cik),
                        observed_at=rec.filed_at,
                        fund_id=fund.id,
                        urgency=Urgency(launch_defaults["default_urgency"]),
                        payload={
                            "accession": rec.accession,
                            "issuer": rec.issuer_name,
                            "declared_fund_type": rec.fund_type,
                            "offering_usd": rec.total_offering_usd,
                        },
                    ),
                    source_run_id=ctx.run_id,
                )
                ctx.result.emit(launch.id)
                ctx.logger.info("new_fund_launch", issuer=rec.issuer_name, cik=rec.cik)
        except Exception as exc:
            ctx.result.error("filing", exc, accession=rec.accession, issuer=rec.issuer_name)
            ctx.logger.error("filing_failed", accession=rec.accession, error=str(exc))

    return ctx.result.build(fund_ids=fund_ids, skipped=skipped)
