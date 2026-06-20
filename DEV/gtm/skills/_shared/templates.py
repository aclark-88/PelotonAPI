"""Template-based LinkedIn copy — headless drafting with no LLM.

Lets the cloud morning sweep draft and auto-send with zero human and zero
Anthropic key. Two tracks, per the doctrine:
  - clarion_pms  (Clarion-fit funds): pain-led pitch, varied by the buyer's
    seat (ops/finance -> close & P&L; tech -> Clarion API; CIO/founder ->
    platform), asking for a meeting.
  - network_value (non-fit funds): Alex's standing offer — cap intro,
    outsourced CFO/CTO, fund-infrastructure referrals — never a product pitch.

Every line obeys the voice contract (<=300 chars, no em dashes, no banned
phrases, no incumbent names) and is run through the same validator before
storage. To upgrade template copy to bespoke LLM copy later, set
ANTHROPIC_API_KEY and the drafter's LLM path takes over.
"""

from __future__ import annotations

from typing import Any

STRATEGY_LABEL = {
    "structured_credit": "structured credit",
    "credit": "credit",
    "fixed_income": "fixed income",
    "macro": "global macro",
    "relative_value": "relative value",
    "multi_strategy": "multi-strategy",
    "volatility_arb": "volatility",
    "convertible_arb": "convertible arbitrage",
    "quant": "systematic",
    "commodities": "commodities",
    "event_driven": "event-driven",
    "equity_long_short": "long/short equity",
}


def strategy_phrase(strategies: list[str]) -> str:
    for s in strategies or []:
        if s in STRATEGY_LABEL:
            return STRATEGY_LABEL[s]
    return "multi-asset"


def build_copy(fund_strategies: list[str], role_function: str, angle_key: str) -> dict[str, Any]:
    """Return {'cr_variants': [...], 'followup': '...'} for one target."""
    if angle_key == "network_value":
        cr = (
            "Hey {{firstName}}, I work closely with launch funds at Coremont, the "
            "PMS spun out of Brevan Howard. Even if the system question is answered, "
            "happy to be a resource or make intros for cap intro, an outsourced "
            "CFO/CTO, or fund infrastructure when those are live."
        )
        followup = (
            "Thanks for connecting. Standing offer: if cap intro, outsourced "
            "CFO/CTO, or fund infrastructure questions come up, I know who is good "
            "and am glad to make the intro. And if the system conversation ever "
            "reopens, you know where I am."
        )
        return {"cr_variants": [cr], "followup": followup}

    sp = strategy_phrase(fund_strategies)
    if role_function == "tech":
        cr = (
            f"Hey {{{{firstName}}}}, building the pricing and risk engine for a {sp} "
            "book in-house is a multi-year project. Clarion API puts that production "
            "engine behind your own stack, with Python SDK access. Would value "
            f"connecting on {sp} analytics infra."
        )
        followup = (
            "Thanks for connecting. The pricing and risk engine is usually the piece "
            "teams least want to build and most need, all via API behind your front "
            "end. If that is on the roadmap as the book scales, open to 15 minutes?"
        )
    elif role_function in ("investment", "executive"):
        cr = (
            f"Hey {{{{firstName}}}}, scaling a {sp} platform, the hard part is one "
            "real-time risk and P&L view across the whole book. Clarion was built "
            "inside Brevan Howard as exactly that book of record. Would value "
            "connecting on platform infrastructure."
        )
        followup = (
            f"Thanks for connecting. As a {sp} platform scales, the consolidated "
            "risk and P&L view tends to strain first, right when allocators look "
            "hardest. Clarion is that book of record, middle office run by Coremont. "
            "Open to 15 minutes?"
        )
    else:  # ops, finance, risk, default
        cr = (
            f"Hey {{{{firstName}}}}, pricing and risk on a {sp} book rarely live in "
            "one place: Clarion PMS runs live P&L and intraday risk across the whole "
            "book, built inside Brevan Howard. Would value connecting to compare "
            f"notes on {sp} risk aggregation."
        )
        followup = (
            f"Thanks for connecting. New {sp} vehicles tend to hit the same wall: "
            "positions priced in several places and a P&L that lags the book right "
            "when allocators look hardest. Clarion is the single live book of record, "
            "middle office run by Coremont. Open to 15 minutes?"
        )
    return {"cr_variants": [cr], "followup": followup}
