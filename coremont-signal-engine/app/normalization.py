"""Entity normalization (Job 2 logic, reusable + unit-testable).

One advisory platform shows up across many legal entity names: master funds,
offshore feeders, parallel and opportunities vehicles. This module collapses
those variants to a stable manager key and classifies each vehicle's structure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Legal suffixes / entity-form noise to strip when deriving the platform key.
_ENTITY_NOISE = [
    "limited partnership",
    "limited liability company",
    "l\\.?p\\.?",
    "l\\.?l\\.?c\\.?",
    "ltd\\.?",
    "limited",
    "inc\\.?",
    "incorporated",
    "corp\\.?",
    "corporation",
    "plc",
    "gp",
    "lllp",
    "llp",
    "sicav",
    "spc",
]

# Vehicle-role / structure words that distinguish a *vehicle* from its platform.
# Removing these collapses "Acme Master Fund" and "Acme Offshore Fund" to "acme".
_VEHICLE_NOISE = [
    "master",
    "feeder",
    "offshore",
    "onshore",
    "parallel",
    "intermediate",
    "opportunities",
    "opportunity",
    "trading",
    "qp",
    "institutional",
    "cayman",
    "international",
    "domestic",
    "fund",
    "funds",
    "partners",
    "capital",
    "management",
    "advisors",
    "advisers",
    "asset",
    "investments",
    "investment",
    "the",
    "series",
]

_OFFSHORE_DOMICILES = {
    "cayman islands",
    "cayman",
    "bermuda",
    "british virgin islands",
    "bvi",
    "luxembourg",
    "ireland",
    "jersey",
    "guernsey",
}

_ROMAN_NUMERAL = re.compile(r"\b[ivxl]+\b")


def _strip_terms(text: str, terms: list[str]) -> str:
    for term in terms:
        text = re.sub(r"\b" + term + r"\b", " ", text, flags=re.IGNORECASE)
    return text


def _clean(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_entity_name(name: str) -> str:
    """Light normalization for storage/display matching (keeps the vehicle words)."""
    cleaned = _clean(name)
    cleaned = _strip_terms(cleaned, _ENTITY_NOISE)
    return re.sub(r"\s+", " ", cleaned).strip()


def manager_key(name: str) -> str:
    """Aggressive key that collapses a manager's many vehicles into one platform.

    Example: "Meridian Structured Credit Offshore Fund Ltd" and
    "Meridian Structured Credit Master Fund LP" both → "meridian structured credit".
    """
    cleaned = _clean(name)
    cleaned = _strip_terms(cleaned, _ENTITY_NOISE)
    cleaned = _strip_terms(cleaned, _VEHICLE_NOISE)
    cleaned = _ROMAN_NUMERAL.sub(" ", cleaned)
    cleaned = re.sub(r"\b\d+\b", " ", cleaned)  # drop standalone numbers
    key = re.sub(r"\s+", " ", cleaned).strip()
    # Guard against over-stripping to empty: fall back to the entity-normalized name.
    return key or normalize_entity_name(name)


@dataclass
class VehicleStructure:
    vehicle_type: str
    is_master: bool
    is_feeder: bool
    is_offshore: bool


def classify_vehicle(name: str, jurisdiction: str | None = None) -> VehicleStructure:
    """Infer master/feeder/offshore structure from the name + domicile."""
    low = name.lower()
    juris = (jurisdiction or "").lower().strip()

    is_master = "master" in low
    is_feeder = "feeder" in low or "offshore" in low or "ltd" in low.split()
    is_offshore = (
        any(d in juris for d in _OFFSHORE_DOMICILES)
        or "offshore" in low
        or "cayman" in low
        or "(cayman)" in low
    )

    if is_master:
        vehicle_type = "master"
    elif is_feeder and is_offshore:
        vehicle_type = "offshore_feeder"
    elif is_feeder:
        vehicle_type = "feeder"
    elif "parallel" in low:
        vehicle_type = "parallel"
    else:
        vehicle_type = "standalone"

    return VehicleStructure(
        vehicle_type=vehicle_type,
        is_master=is_master,
        is_feeder=is_feeder,
        is_offshore=is_offshore,
    )
