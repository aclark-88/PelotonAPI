"""adv_fit_scorer — Form ADV enrichment + fit scoring.

Form ADV lives on IAPD (adviserinfo.sec.gov), NOT EDGAR; the EdgarSource
fetches it from the public adviserinfo API. The fetch enriches the fund row
(AUM, strategies where inferable, PB/custodian when disclosed), scores fit
0-100 via the pure scorer in _shared/scoring.py, appends to scoring_runs, and
caches funds.fit_score / funds.tier.

Emits fit_score_changed when |delta| >= icp.fit_score_change_threshold and a
prior score existed.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timezone
from uuid import UUID

from gtm.models.common import Urgency
from gtm.models.funds import Fund, FundIn
from gtm.models.signals import SignalIn
from gtm.skills._shared import dedupe
from gtm.skills._shared.context import SkillContext, SkillResult
from gtm.skills._shared.scoring import FitInputs, score_fund_fit
from gtm.skills._shared.sources import AdvProfile, SourceUnavailable

SKILL_NAME = "adv_fit_scorer"


def _resolve_fund(ctx: SkillContext, fund_id, crd, cik) -> Fund | None:
    if fund_id:
        return ctx.db.funds.get(UUID(str(fund_id)))
    if cik:
        return ctx.db.funds.find_by_cik(str(cik))
    if crd:
        found = ctx.db.funds._find_existing(FundIn(legal_name="_", crd=str(crd)))
        return Fund.model_validate(found) if found else None
    return None


def _infer_strategies(profile: AdvProfile, keywords: dict[str, str]) -> list[str]:
    haystack = f"{profile.firm_name} {profile.raw}".lower()
    hits = {key for token, key in keywords.items() if token.lower() in haystack}
    return sorted(hits)


def run(
    ctx: SkillContext,
    fund_id: str | None = None,
    crd: str | None = None,
    cik: str | None = None,
) -> SkillResult:
    fund = _resolve_fund(ctx, fund_id, crd, cik)
    if fund is None:
        ctx.result.error("resolve", f"no fund found for fund_id={fund_id} crd={crd} cik={cik}")
        return ctx.result.build()
    ctx.result.records_processed = 1

    try:
        edgar = ctx.sources.require("edgar")
    except SourceUnavailable as exc:
        ctx.result.error("sources", exc)
        return ctx.result.build()

    profile: AdvProfile | None = edgar.adv_firm_profile(
        crd=fund.crd or crd, cik=fund.cik or cik, name=fund.legal_name
    )
    if profile is None:
        ctx.logger.info("adv_not_found", fund=str(fund.id), name=fund.legal_name)
        return ctx.result.build(adv_available=False, fund_id=str(fund.id))

    # ── enrich the fund row (never clobber known values with None) ──────────
    strategies = _infer_strategies(profile, ctx.config.get("strategy_keywords", {}))
    update = FundIn(
        legal_name=fund.legal_name,
        crd=profile.crd or fund.crd,
        cik=fund.cik,
        primary_domain=fund.primary_domain or profile.website,
        aum_usd_millions=profile.regulatory_aum_usd or fund.aum_usd_millions,
        aum_as_of=_parse_date(profile.aum_as_of) or fund.aum_as_of,
        strategies=sorted(set(fund.strategies) | set(strategies)),
        prime_brokers=profile.prime_brokers or fund.prime_brokers,
        custodians=profile.custodians or fund.custodians,
        administrator=profile.administrator or fund.administrator,
        headquarters_city=fund.headquarters_city or profile.headquarters_city,
        headquarters_country=fund.headquarters_country or profile.headquarters_country,
    )
    if not ctx.dry_run:
        fund = ctx.db.funds.upsert_fund(update, source_run_id=ctx.run_id)
        ctx.result.records_updated = 1

    # ── score ────────────────────────────────────────────────────────────────
    icp = ctx.config.get("icp", {})
    deriv = (fund.metadata.get("thirteen_f") or {}).get("intensity_score")
    fit = score_fund_fit(
        FitInputs(
            aum_usd_millions=fund.aum_usd_millions,
            strategies=fund.strategies,
            pct_private_fund=profile.pct_private_fund,
            prime_broker_count=len(fund.prime_brokers),
            custodian_count=len(fund.custodians),
            is_emerging_manager=bool(fund.is_emerging_manager),
            derivatives_intensity=deriv,
        ),
        icp,
        clarion=ctx.config.get("clarion"),
    )
    prior_score = fund.fit_score
    ctx.logger.info("scored", fund=str(fund.id), score=fit.score, tier=fit.tier, prior=prior_score)

    if not ctx.dry_run:
        ctx.db.funds.record_fit_score(
            fund_id=fund.id,
            score=fit.score,
            model_version=str(ctx.config.get("model_version", "fit-v1")),
            reasoning=fit.reasoning,
            inputs={"components": fit.components, "adv_crd": profile.crd},
            tier=fit.tier,
            source_run_id=ctx.run_id,
        )

        threshold = int(icp.get("fit_score_change_threshold", 10))
        if prior_score is not None and abs(fit.score - prior_score) >= threshold:
            defaults = ctx.db.signals.type_defaults("fit_score_changed")
            signal = ctx.db.signals.record_signal(
                SignalIn(
                    signal_type="fit_score_changed",
                    source="edgar_tools",
                    source_record_id=dedupe.fit_score_change_record_id(
                        str(fund.id), prior_score, fit.score,
                        datetime.now(timezone.utc).date().isoformat(),
                    ),
                    observed_at=datetime.now(timezone.utc),
                    fund_id=fund.id,
                    urgency=Urgency(defaults["default_urgency"]),
                    payload={
                        "old_score": prior_score,
                        "new_score": fit.score,
                        "tier": fit.tier,
                        "components": fit.components,
                    },
                ),
                source_run_id=ctx.run_id,
            )
            ctx.result.emit(signal.id)

    return ctx.result.build(
        adv_available=True,
        fund_id=str(fund.id),
        fit_score=fit.score,
        tier=fit.tier,
    )


def _parse_date(value: str | None) -> date_type | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    return None
