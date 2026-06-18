"""Form ADV (IAPD) enrichment — runs in the SEC-fetch stage (GitHub runner).

Downloads the SEC's monthly "Information about Registered Investment Advisers"
firm roster and attaches each Form D candidate's adviser profile:

- adv_matched          : the candidate's manager was found in the ADV roster
- adv_hedge_confirmed  : the adviser reports running hedge funds (Any Hedge Funds)
- adv_aum              : regulatory assets under management ($)
- adv_private_funds    : count of private funds (7B(1)) — platform size
- adv_total_gav        : total gross assets of private funds ($)
- adv_status / adv_status_date / adv_filing_date
- new_registration     : adviser's SEC status took effect recently (a launch)
- adv_fund_mix         : {hedge, pe, re, vc, ...} fund counts (disambiguation)

These are the "infrastructure inflection" signals: a newly-registered adviser is
evaluating its operating platform for the first time; a growing AUM / fund count
is a scaler facing operational due diligence.

Schema confirmed by tools/adv_probe.py against the real file. Columns are
resolved by keyword so minor header changes don't break matching.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import zipfile
from typing import Any

import urllib.request

UA = "Coremont Clarion Prospecting malex.clark@gmail.com"
_BASE = (
    "https://www.sec.gov/files/investment/data/other/"
    "information-about-registered-investment-advisers-exempt-reporting-advisers/"
)
# Two monthly rosters cover the universe: the registered-RIA file (ia{MMDDYY}.zip)
# and the (larger) exempt-reporting-adviser file (ia{MMDDYYYY}-exempt.zip).
NEW_REGISTRATION_DAYS = 270  # adviser SEC status effective within ~9 months = launch

_SUFFIXES = re.compile(
    r"\b(l\.?p\.?|l\.?l\.?c\.?|l\.?l\.?l\.?p\.?|ltd|inc|gp|co|corp|llp)\b", re.I
)


def _norm_entity(name: str) -> str:
    """Normalize an entity name for exact-ish matching across Form D and ADV."""
    s = (name or "").lower()
    s = _SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def _month_files(y: int, m: int) -> list[str]:
    """Both roster file URLs for the 1st of a given month."""
    return [
        _BASE + f"ia{m:02d}01{y % 100:02d}.zip",
        _BASE + f"ia{m:02d}01{y:04d}-exempt.zip",
    ]


def _fetch(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except Exception:  # noqa: BLE001
        return None


def _download_rosters(today: dt.date) -> list[bytes]:
    """Download both roster files for the most recent month that has any."""
    y, m = today.year, today.month
    for _ in range(4):  # current month-first + 3 prior
        blobs = [b for b in (_fetch(u) for u in _month_files(y, m)) if b]
        if blobs:
            return blobs
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return []


def _find_col(cols: list[str], *needles: str) -> str | None:
    low = {c.lower().strip(): c for c in cols}
    for c_low, c in low.items():
        if all(n in c_low for n in needles):
            return c
    return None


def _to_float(v: Any) -> float | None:
    try:
        s = re.sub(r"[^0-9.]", "", str(v))
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


def _brand(name: str, n_tokens: int) -> str:
    """First N significant tokens of a normalized name = the manager 'brand'."""
    toks = _norm_entity(name).split()
    return " ".join(toks[:n_tokens]) if len(toks) >= n_tokens else ""


def build_index(zip_blobs: bytes | list[bytes]) -> dict[str, Any]:
    """Parse one or more ADV roster zips into a merged index.

    Returns {by_cik, by_name, by_brand, count}. ``by_brand`` maps a manager brand
    (first 1 and first 2 normalized tokens of the adviser name) to a record, but
    ONLY when that brand is unique across all advisers — ambiguous brands map to
    None so we never make a wrong guess.
    """
    if isinstance(zip_blobs, (bytes, bytearray)):
        zip_blobs = [zip_blobs]
    by_cik: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    brand_owner: dict[str, str] = {}   # brand -> normalized adviser name (or "*" if many)
    brand_rec: dict[str, dict] = {}
    count = 0
    for blob in zip_blobs:
        try:
            count += _index_one(blob, by_cik, by_name, brand_owner, brand_rec)
        except Exception:  # noqa: BLE001 - skip a bad file, keep the rest
            continue
    by_brand = {b: brand_rec[b] for b, owner in brand_owner.items() if owner != "*" and len(b) >= 4}
    return {"by_cik": by_cik, "by_name": by_name, "by_brand": by_brand, "count": count}


# Generic first-words that must never be used as a 1-token brand match.
_GENERIC_BRANDS = {
    "the", "new", "global", "prime", "select", "core", "alpha", "capital",
    "fund", "asset", "investment", "investments", "partners", "advisors",
    "advisers", "management", "group", "us", "american", "first", "north",
}


def _brands_of(name: str) -> list[str]:
    """Candidate brand keys for a fund/adviser name (2-token first, then 1-token
    if distinctive)."""
    out = []
    b2 = _brand(name, 2)
    if b2:
        out.append(b2)
    b1 = _brand(name, 1)
    if b1 and b1 not in _GENERIC_BRANDS and len(b1) >= 4:
        out.append(b1)
    return out


def _index_one(zip_bytes: bytes, by_cik: dict, by_name: dict, brand_owner: dict, brand_rec: dict) -> int:
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    csv_name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
    if not csv_name:
        return 0
    text = zf.read(csv_name).decode("latin-1", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])

    col = {
        "cik": _find_col(header, "cik"),
        "crd": _find_col(header, "organization", "crd"),
        "primary": _find_col(header, "primary business name"),
        "legal": _find_col(header, "legal name"),
        "status": _find_col(header, "sec current status"),
        "status_date": _find_col(header, "sec status effective date"),
        "filing_date": _find_col(header, "latest adv filing date"),
        "aum": _find_col(header, "amount of assets"),
        "pf_count": _find_col(header, "count of private funds", "7b(1)"),
        "gav": _find_col(header, "gross assets of private funds"),
        "any_hedge": _find_col(header, "any hedge funds"),
        "n_hedge": _find_col(header, "number of hedge funds"),
        "n_pe": _find_col(header, "number of pe funds"),
        "n_re": _find_col(header, "number of real estate funds"),
        "n_vc": _find_col(header, "number of vc funds"),
        "city": _find_col(header, "main office city"),
        "state": _find_col(header, "main office state"),
    }
    idx = {header.index(v): k for k, v in col.items() if v is not None}

    n = 0
    for row in reader:
        if not row:
            continue
        rec: dict[str, Any] = {}
        for pos, key in idx.items():
            rec[key] = row[pos] if pos < len(row) else None
        n += 1
        cik = re.sub(r"\D", "", str(rec.get("cik") or "")).lstrip("0")
        if cik:
            by_cik.setdefault(cik, rec)
        adviser_norm = _norm_entity(rec.get("primary") or rec.get("legal") or "")
        for nm in (rec.get("primary"), rec.get("legal")):
            k = _norm_entity(nm or "")
            if k:
                by_name.setdefault(k, rec)
        # brand index with ambiguity tracking
        brands = set(_brands_of(rec.get("primary") or "")) | set(_brands_of(rec.get("legal") or ""))
        for b in brands:
            owner = brand_owner.get(b)
            if owner is None:
                brand_owner[b] = adviser_norm
                brand_rec[b] = rec
            elif owner != adviser_norm:
                brand_owner[b] = "*"  # used by >1 adviser -> never match on it
    return n


def _match(cand: dict[str, Any], index: dict[str, Any]) -> dict | None:
    cik = re.sub(r"\D", "", str(cand.get("cik") or "")).lstrip("0")
    if cik and cik in index["by_cik"]:
        return index["by_cik"][cik]
    names = list(cand.get("related_persons") or []) + [cand.get("fund", "")]
    # 1) exact normalized name match (highest precision)
    for nm in names:
        k = _norm_entity(nm)
        if k and k in index["by_name"]:
            return index["by_name"][k]
    # 2) unique-brand match: the fund/manager brand maps to exactly one adviser
    by_brand = index.get("by_brand", {})
    for nm in names:
        for b in _brands_of(nm):
            if b in by_brand:
                return by_brand[b]
    return None


def _signals_from(rec: dict[str, Any], today: dt.date) -> dict[str, Any]:
    aum = _to_float(rec.get("aum"))
    gav = _to_float(rec.get("gav"))
    n_hedge = _to_float(rec.get("n_hedge")) or 0
    any_hedge = str(rec.get("any_hedge") or "").strip().lower() in ("y", "yes", "true", "1")
    # newly registered?
    new_reg = False
    sd = str(rec.get("status_date") or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            d = dt.datetime.strptime(sd, fmt).date()
            new_reg = (today - d).days <= NEW_REGISTRATION_DAYS and (today - d).days >= 0
            break
        except ValueError:
            continue
    return {
        "adv_matched": True,
        "adv_name": rec.get("primary") or rec.get("legal"),
        "adv_crd": rec.get("crd"),
        "adv_status": rec.get("status"),
        "adv_status_date": sd,
        "adv_aum": aum,
        "adv_total_gav": gav,
        "adv_private_funds": _to_float(rec.get("pf_count")),
        "adv_hedge_confirmed": any_hedge or n_hedge > 0,
        "adv_fund_mix": {
            "hedge": _to_float(rec.get("n_hedge")),
            "pe": _to_float(rec.get("n_pe")),
            "re": _to_float(rec.get("n_re")),
            "vc": _to_float(rec.get("n_vc")),
        },
        "new_registration": new_reg,
    }


def enrich(candidates: list[dict[str, Any]], today: dt.date | None = None) -> dict[str, Any]:
    """Download the ADV roster and annotate candidates in place. Returns coverage."""
    today = today or dt.date.today()
    blobs = _download_rosters(today)
    if not blobs:
        return {"ok": False, "matched": 0, "advisers": 0}
    try:
        index = build_index(blobs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "matched": 0, "advisers": 0, "error": str(exc)}

    matched = 0
    for c in candidates:
        rec = _match(c, index)
        if rec:
            c.update(_signals_from(rec, today))
            matched += 1
        else:
            c["adv_matched"] = False
    return {"ok": True, "matched": matched, "advisers": index.get("count", 0)}
