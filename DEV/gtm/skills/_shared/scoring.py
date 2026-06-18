"""Fit scoring — pure functions, no I/O.

All weights and thresholds come from configs/icp.yaml; nothing is hardcoded.
The same scorer feeds adv_fit_scorer today and any future rescore job.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FitInputs(BaseModel):
    aum_usd_millions: float | None = None
    strategies: list[str] = Field(default_factory=list)
    pct_private_fund: float | None = None      # derivatives-intensity proxy input
    prime_broker_count: int = 0
    custodian_count: int = 0
    is_emerging_manager: bool = False
    derivatives_intensity: float | None = None  # 0-100 if 13F scorer has run


class FitScore(BaseModel):
    score: int
    tier: int
    components: dict[str, float]
    reasoning: str


def _aum_band(aum_usd_millions: float | None, bands: list[dict[str, Any]]) -> str:
    if aum_usd_millions is None:
        return "unknown"
    for band in bands:
        lo = band.get("min", 0) or 0
        hi = band.get("max")
        if aum_usd_millions >= lo and (hi is None or aum_usd_millions < hi):
            return band["key"]
    return "unknown"


def clarion_coverage_for(strategies: list[str], clarion: dict[str, Any]) -> list[str]:
    """Asset classes from configs/clarion_coverage.yaml matching the fund's
    strategies — the capability evidence behind a strategy-fit claim."""
    matched = []
    for asset_class, spec in (clarion.get("asset_classes") or {}).items():
        if set(spec.get("strategies", [])) & set(strategies):
            matched.append(asset_class)
    return matched


def score_fund_fit(
    inputs: FitInputs, icp: dict[str, Any], clarion: dict[str, Any] | None = None
) -> FitScore:
    """Weighted 0-100 score per configs/icp.yaml `scoring:` block. When the
    Clarion coverage config is provided, reasoning cites which Clarion asset
    classes back the strategy-fit claim."""
    weights = icp.get("scoring", {})
    components: dict[str, float] = {}
    notes: list[str] = []

    # AUM band fit
    band_scores: dict[str, float] = weights.get("aum_band_scores", {})
    band = _aum_band(inputs.aum_usd_millions, icp.get("aum_bands", []))
    components["aum_band"] = float(band_scores.get(band, 0))
    notes.append(f"AUM band '{band}' -> {components['aum_band']}")

    # Strategy fit: best matching strategy wins, half-credit for each extra match
    strategy_weights: dict[str, float] = weights.get("strategy_scores", {})
    matched = sorted(
        (float(strategy_weights.get(s, 0)) for s in inputs.strategies), reverse=True
    )
    strategy_score = 0.0
    if matched:
        strategy_score = matched[0] + 0.5 * sum(matched[1:])
    cap = float(weights.get("strategy_cap", 30))
    components["strategy"] = min(strategy_score, cap)
    notes.append(f"strategies {inputs.strategies} -> {components['strategy']}")
    if clarion and inputs.strategies:
        covered = clarion_coverage_for(inputs.strategies, clarion)
        if covered:
            notes.append(f"Clarion coverage match: {', '.join(covered)} (per Product Coverage 2026)")

    # Derivatives / complexity proxy
    deriv_weight = float(weights.get("derivatives_weight", 15))
    if inputs.derivatives_intensity is not None:
        components["derivatives"] = deriv_weight * (inputs.derivatives_intensity / 100.0)
        notes.append(f"13F intensity {inputs.derivatives_intensity} -> {components['derivatives']:.1f}")
    elif inputs.pct_private_fund is not None:
        components["derivatives"] = deriv_weight * min(inputs.pct_private_fund, 1.0)
        notes.append(f"private-fund share {inputs.pct_private_fund:.0%} (proxy) -> {components['derivatives']:.1f}")
    else:
        components["derivatives"] = 0.0
        notes.append("no derivatives evidence yet -> 0")

    # Multi-PB / multi-custodian operational complexity bonuses
    if inputs.prime_broker_count >= int(weights.get("multi_pb_min", 2)):
        components["multi_pb"] = float(weights.get("multi_pb_bonus", 10))
        notes.append(f"{inputs.prime_broker_count} prime brokers -> +{components['multi_pb']}")
    if inputs.custodian_count >= int(weights.get("multi_custodian_min", 2)):
        components["multi_custodian"] = float(weights.get("multi_custodian_bonus", 5))
        notes.append(f"{inputs.custodian_count} custodians -> +{components['multi_custodian']}")

    # Emerging manager flag
    if inputs.is_emerging_manager:
        components["emerging"] = float(weights.get("emerging_manager_bonus", 10))
        notes.append(f"emerging manager -> +{components['emerging']}")

    score = int(round(min(max(sum(components.values()), 0), 100)))
    tier = tier_for_score(score, icp)
    return FitScore(
        score=score,
        tier=tier,
        components=components,
        reasoning="; ".join(notes) + f" | total {score} -> tier {tier}",
    )


def tier_for_score(score: int, icp: dict[str, Any]) -> int:
    """Tier 1 is best. Thresholds from icp.yaml tiers: {1: 75, 2: 55, 3: 35}."""
    thresholds: dict[Any, Any] = icp.get("tiers", {1: 75, 2: 55, 3: 35})
    for tier in sorted(int(t) for t in thresholds):
        if score >= float(thresholds[tier] if tier in thresholds else thresholds[str(tier)]):
            return tier
    return 4
