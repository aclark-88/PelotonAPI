"""Likely Clarion buyer personas and how Clarion pain maps to each filing signal."""
from __future__ import annotations

# Personas we map managers to for outreach (suggested by the build brief).
PERSONAS = [
    ("coo", "Chief Operating Officer", ["coo", "chief operating", "operating officer"]),
    ("operations", "Head of Operations", ["operations", "ops", "fund operations"]),
    ("risk", "Head of Risk", ["risk", "cro", "chief risk"]),
    ("finance", "CFO / Finance", ["cfo", "finance", "controller", "chief financial"]),
    ("treasury", "Treasury", ["treasury", "treasurer", "financing", "collateral"]),
    ("platform", "Platform / Technology", ["platform", "head of technology", "cto", "engineering"]),
]

# Default outreach order — the operational buyers who feel platform strain first.
DEFAULT_PERSONA_ORDER = ["coo", "operations", "risk", "treasury", "finance"]

# Clarion pain framing per signal, used in "Why Coremont now?" copy.
PAIN_BY_SIGNAL = {
    "new_fund_launch": "a fresh operating build-out where real-time risk and P&L tooling is decided early",
    "active_capital_raise": "rising AUM that stresses cross-book P&L and investor-grade reporting",
    "ongoing_raise_momentum": "a live vehicle scaling allocations and trade volume",
    "platform_expansion": "more vehicles and books to consolidate for firm-wide risk visibility",
    "strategy_expansion": "new instrument complexity in derivatives, financing, and treasury workflows",
    "structural_complexity": "master/feeder and offshore structures that fragment risk and cash visibility",
}


def classify_title(title: str | None) -> str | None:
    """Map a free-text job title to one of our personas, if possible."""
    if not title:
        return None
    low = title.lower()
    for key, _label, needles in PERSONAS:
        if any(n in low for n in needles):
            return key
    return None


def label_for(persona_key: str) -> str:
    for key, label, _ in PERSONAS:
        if key == persona_key:
            return label
    return persona_key
