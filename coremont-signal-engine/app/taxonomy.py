"""Weighted keyword taxonomy for Clarion-fit scoring.

We deliberately use a *weighted* dictionary rather than a binary filter so a
manager can still surface and rank even when a filing is sparse. Each term maps
to a point contribution toward the 30-point strategy-fit bucket; negative terms
penalise low-fit profiles (venture, long-only, retail, etc.).

Matching is done on lower-cased, normalized text and respects word boundaries so
short tokens (e.g. "abs", "clo") don't match inside unrelated words.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Strong positive — high overlap with Clarion's multi-strat / macro / credit ICP.
STRONG_POSITIVE: dict[str, int] = {
    "multi-strategy": 15,
    "multi strategy": 15,
    "multistrategy": 15,
    "global macro": 15,
    "structured credit": 15,
    "macro": 12,
    "fixed income": 12,
    "rates": 12,
    "relative value": 12,
    "opportunistic credit": 12,
    "rmbs": 12,
    "cmbs": 12,
    "clo": 12,
    "securitized": 12,
    "credit": 10,
    "abs": 10,
    "mbs": 10,
    "volatility": 10,
    "derivatives": 10,
    "mortgage": 8,
}

# Medium positive — supporting signals of complexity / structure / breadth.
MEDIUM_POSITIVE: dict[str, int] = {
    "alternative credit": 8,
    "special situations": 6,
    "unconstrained": 6,
    "tactical": 5,
    "offshore fund": 5,
    "master fund": 5,
    "feeder fund": 5,
    "trading fund": 5,
}

# Negative / low-fit — penalise profiles outside Clarion's sweet spot.
NEGATIVE: dict[str, int] = {
    "venture": -12,
    "buyout": -10,
    "private equity": -8,
    "long-only": -10,
    "long only": -10,
    "single-asset real estate": -10,
    "passive": -8,
    "etf": -8,
    "index fund": -8,
    "retail": -6,
}

# Tags we surface on the manager when a strong/medium positive term matches.
# Maps the matched keyword to a clean, de-duplicated display tag.
_TAG_FOR_TERM: dict[str, str] = {
    "multi-strategy": "multi-strategy",
    "multi strategy": "multi-strategy",
    "multistrategy": "multi-strategy",
    "global macro": "macro",
    "macro": "macro",
    "fixed income": "fixed income",
    "rates": "rates",
    "relative value": "relative value",
    "structured credit": "structured credit",
    "opportunistic credit": "opportunistic credit",
    "alternative credit": "alternative credit",
    "credit": "credit",
    "rmbs": "RMBS",
    "cmbs": "CMBS",
    "abs": "ABS",
    "mbs": "MBS",
    "clo": "CLO",
    "securitized": "securitized",
    "mortgage": "mortgage",
    "volatility": "volatility",
    "derivatives": "derivatives",
    "special situations": "special situations",
}

ALL_TERMS: dict[str, int] = {**STRONG_POSITIVE, **MEDIUM_POSITIVE, **NEGATIVE}

# Pre-compiled word-boundary patterns, longest term first so multi-word phrases
# win over their constituent single words.
_PATTERNS: list[tuple[str, int, re.Pattern]] = [
    (term, weight, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for term, weight in sorted(ALL_TERMS.items(), key=lambda kv: -len(kv[0]))
]


@dataclass
class TaxonomyMatch:
    matched: dict[str, int] = field(default_factory=dict)  # term -> weight
    tags: list[str] = field(default_factory=list)

    @property
    def positive_weight(self) -> int:
        return sum(w for w in self.matched.values() if w > 0)

    @property
    def negative_weight(self) -> int:
        return sum(w for w in self.matched.values() if w < 0)


def match_text(text: str | None) -> TaxonomyMatch:
    """Scan free text and return matched terms, their weights, and display tags."""
    result = TaxonomyMatch()
    if not text:
        return result
    seen_tags: list[str] = []
    for term, weight, pattern in _PATTERNS:
        if pattern.search(text):
            result.matched[term] = weight
            tag = _TAG_FOR_TERM.get(term)
            if tag and tag not in seen_tags:
                seen_tags.append(tag)
    result.tags = seen_tags
    return result
