"""Consistent source_record_id construction.

signals.dedupe_key is generated in Postgres from
md5(source:source_record_id:signal_type); idempotency therefore hinges on
every skill building source_record_id the same deterministic way. These
helpers are that single way. Never inline-format a record id in a skill.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def form_d_record_id(accession: str) -> str:
    """A Form D filing event — one per accession number."""
    return f"formd:{accession}"


def form_d_launch_record_id(cik: str) -> str:
    """First-ever Form D = launch. Keyed to the issuer, not the accession, so
    a later amendment can never produce a second 'launch'."""
    return f"launch:cik:{cik}"


def fit_score_change_record_id(fund_id: str, old: int | None, new: int, as_of: str) -> str:
    return f"fitscore:{fund_id}:{old}->{new}:{as_of}"


def thirteen_f_record_id(cik: str, period: str) -> str:
    return f"13f:{cik}:{period}"


def web_finding_record_id(topic: str, url: str) -> str:
    """A web-search finding: stable on the canonical URL, not the snippet."""
    digest = hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()[:16]
    return f"web:{topic}:{digest}"


def content_fingerprint(payload: Any) -> str:
    """sha256 over canonical JSON — same convention as raw_payloads hashing."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
