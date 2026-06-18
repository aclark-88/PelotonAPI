"""thirteen_f_intensity_scorer — derivatives/complexity scoring from 13F.

Pulls up to 4 quarters of 13F-HR for the fund's CIK, computes position count,
QoQ turnover, options presence, and top-10 concentration, writes the result
to funds.metadata.thirteen_f, and emits derivatives_intensity_high when the
0-100 intensity crosses the configured threshold.

Most sub-$3.5B hedge funds either don't file or file via a family aggregator;
when no 13F exists the skill records metadata.thirteen_f.available=false and
exits success — absence of a filing is not an error.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from gtm.models.common import Urgency
from gtm.models.signals import SignalIn
from gtm.skills._shared import dedupe
from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.sources import SourceUnavailable, ThirteenFSnapshot

SKILL_NAME = "thirteen_f_intensity_scorer"


def compute_intensity(snapshots: list[ThirteenFSnapshot], cfg: dict[str, Any]) -> dict[str, Any]:
    """Pure: latest-quarter metrics + 0-100 intensity per config weights."""
    weights = cfg.get("weights", {})
    latest = snapshots[0]
    prior = snapshots[1] if len(snapshots) > 1 else None

    options_share = (
        latest.option_position_count / latest.position_count
        if latest.position_count
        else 0.0
    )
    turnover = None
    if prior and prior.positions and latest.positions:
        latest_set, prior_set = set(latest.positions), set(prior.positions)
        turnover = len(latest_set ^ prior_set) / max(len(latest_set | prior_set), 1)

    components: dict[str, float] = {}
    options_full = float(cfg.get("options_share_full", 0.15))
    components["options_share"] = float(weights.get("options_share", 50)) * min(
        options_share / options_full, 1.0
    )
    components["turnover"] = float(weights.get("turnover", 30)) * (turnover or 0.0)
    floor = int(cfg.get("position_count_floor", 100))
    components["position_count"] = float(weights.get("position_count", 10)) * min(
        latest.position_count / floor, 1.0
    )
    conc = latest.top10_concentration
    components["concentration_inverse"] = (
        float(weights.get("concentration_inverse", 10)) * (1.0 - min(conc, 1.0))
        if conc is not None
        else 0.0
    )

    score = int(round(min(sum(components.values()), 100)))
    return {
        "available": True,
        "intensity_score": score,
        "components": components,
        "latest_period": latest.period,
        "position_count": latest.position_count,
        "options_share": round(options_share, 4),
        "turnover": round(turnover, 4) if turnover is not None else None,
        "top10_concentration": round(conc, 4) if conc is not None else None,
        "quarters_analyzed": len(snapshots),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def run(ctx: SkillContext, fund_id: str) -> SkillResult:
    fund = ctx.db.funds.get(UUID(str(fund_id)))
    if fund is None:
        ctx.result.error("resolve", f"fund {fund_id} not found")
        return ctx.result.build()
    ctx.result.records_processed = 1

    if not fund.cik:
        ctx.logger.info("no_cik", fund=str(fund.id))
        if not ctx.dry_run:
            ctx.db.funds.update_metadata(fund.id, {"thirteen_f": {"available": False, "reason": "no_cik"}})
            ctx.result.records_updated = 1
        return ctx.result.build(thirteen_f_available=False)

    try:
        edgar = ctx.sources.require("edgar")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build()

    snapshots = edgar.thirteen_f_quarters(fund.cik, quarters=int(ctx.config.get("quarters", 4)))
    if not snapshots:
        ctx.logger.info("no_13f", fund=str(fund.id), cik=fund.cik)
        if not ctx.dry_run:
            ctx.db.funds.update_metadata(
                fund.id, {"thirteen_f": {"available": False, "reason": "no_filings"}}
            )
            ctx.result.records_updated = 1
        return ctx.result.build(thirteen_f_available=False)

    analysis = compute_intensity(snapshots, ctx.config)
    ctx.logger.info("intensity", fund=str(fund.id), score=analysis["intensity_score"])

    if not ctx.dry_run:
        ctx.db.funds.update_metadata(fund.id, {"thirteen_f": analysis})
        ctx.result.records_updated = 1

        if analysis["intensity_score"] >= int(ctx.config.get("signal_threshold", 60)):
            defaults = ctx.db.signals.type_defaults("derivatives_intensity_high")
            signal = ctx.db.signals.record_signal(
                SignalIn(
                    signal_type="derivatives_intensity_high",
                    source="edgar_tools",
                    source_record_id=dedupe.thirteen_f_record_id(fund.cik, analysis["latest_period"]),
                    observed_at=datetime.now(timezone.utc),
                    fund_id=fund.id,
                    urgency=Urgency(defaults["default_urgency"]),
                    payload={k: v for k, v in analysis.items() if k != "components"},
                ),
                source_run_id=ctx.run_id,
            )
            ctx.result.emit(signal.id)

    return ctx.result.build(
        thirteen_f_available=True, intensity_score=analysis["intensity_score"]
    )
