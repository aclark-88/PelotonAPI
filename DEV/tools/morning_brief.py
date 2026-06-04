"""Morning Brief — the daily buying-signal dashboard.

Run this each weekday morning. It scans recent SEC EDGAR activity for the
operational buying signals that map to a Coremont Clarion conversation, ranks
them, persists state so "what's new / what changed" is meaningful over time, and
renders a self-contained HTML dashboard you can just open.

Signals
-------
- greenfield_launch : a NEW pooled-investment **hedge fund** Form D notice
  (ICP-filtered to Hedge Fund / Other Investment Fund — VC / PE / real-estate
  noise is dropped). Continuous; the daily bread-and-butter.
- aum_growth        : a tracked manager's latest 13F total value is materially
  up vs. the prior 13F we recorded. Quarterly (clusters around 13F deadlines).
- derivatives_complex : a tracked manager's options (Put/Call) share of the book
  crossed/rose past 15% vs. the prior 13F — new operational complexity.

"Auto-discover + track": the brief snapshots every 13F manager it sees in the
lookback window (plus any in config/watchlist.txt) into ``manager_snapshots``,
so the tracked universe — and the baselines needed for growth/derivatives
signals — build themselves over time. The first time a manager is seen is a
baseline only (no signal yet).

Output
------
- briefs/latest.html  (+ a dated copy) — the dashboard
- briefs/latest.json  — machine-readable signals
- memory.db           — entities/observations (High/Medium -> QUALIFIED so you
                        can immediately `sales_copilot.py --crd <cik>`)

No network spend (free EDGAR). Honors EDGAR_IDENTITY + the lookback/scan caps.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:  # pragma: no cover
    pass

import edgar

import adv_enrich
import sec_parser
from _shared import (
    BRIEFS_DIR,
    CONFIG_DIR,
    DB_PATH,
    ensure_dirs,
    fatal,
    ok,
    retry,
    run_cli,
)
from db_client import add_observation, log_execution, set_status, upsert_entity
from sales_copilot import COPY_BLOCKS

# --- tuning ---------------------------------------------------------------
AUM_GROWTH_THRESHOLD = 0.15          # +15% QoQ total value
OPTIONS_THRESHOLD = sec_parser.OPTIONS_CONCENTRATION_THRESHOLD  # 0.15
DEFAULT_FORMD_LOOKBACK = 4           # days; covers weekends
DEFAULT_13F_LOOKBACK = 7             # days
DEFAULT_FORMD_CAP = 250              # max Form D objs to deep-scan per run
DEFAULT_13F_CAP = 60                 # max 13F filings to parse per run

# --- ICP classifier (config-driven) ---------------------------------------
# Form D has no strategy field, so a fund's fit is inferred from its NAME + the
# Form D fund type, using lexicons in config/icp_filters.json (editable).
_DEFAULT_FILTERS: dict[str, Any] = {
    "exclude_fund_types": ["Private Equity Fund", "Venture Capital Fund"],
    "require_strategy_match": True,
    "negative_terms": [
        "real estate", "realty", "property", "development", "housing",
        "infrastructure", "energy", "private equity", "private credit",
        "direct lending", "venture", "buyout", "equity partners", "mezzanine",
        "spv", "royalty", "bdc",
    ],
    "positive_terms": {
        "global macro": 15, "macro": 9, "relative value": 15, "fixed income": 12,
        "rates": 9, "structured credit": 16, "securitized": 12, "clo": 12,
        "credit": 6, "distressed": 10, "convertible": 12, "arbitrage": 12,
        "volatility": 11, "derivatives": 12, "systematic": 10, "quant": 9,
        "multi-strategy": 14, "market neutral": 13, "absolute return": 10,
    },
    "tiers": {"high": 78, "medium": 60},
}


def _load_filters() -> dict[str, Any]:
    path = CONFIG_DIR / "icp_filters.json"
    if not path.exists():
        return _DEFAULT_FILTERS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # fall back to defaults for any missing key
        return {**_DEFAULT_FILTERS, **{k: v for k, v in data.items() if not k.startswith("_")}}
    except (json.JSONDecodeError, OSError):
        return _DEFAULT_FILTERS


def _load_verifications() -> dict[str, Any]:
    """Manager-verification overrides (cik -> {is_target, business, ...}).

    Authoritative: a verdict here beats the name/type heuristics. Written by the
    verification step after checking what a manager actually is (web / ADV).
    """
    path = CONFIG_DIR / "verifications.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}
    except (json.JSONDecodeError, OSError):
        return {}


def _norm_cik(cik: str) -> str:
    s = str(cik).strip()
    return str(int(s)) if s.isdigit() else s


FILTERS = _load_filters()
VERIFICATIONS = _load_verifications()
import re as _re

_TERM_CACHE: dict[str, Any] = {}


def _term_hits(text: str, terms) -> list[str]:
    """Return which `terms` appear in `text` on word boundaries (lowercased)."""
    low = (text or "").lower()
    hits = []
    for t in terms:
        rx = _TERM_CACHE.get(t)
        if rx is None:
            rx = _TERM_CACHE[t] = _re.compile(r"(?<![a-z0-9])" + _re.escape(t.lower()) + r"(?![a-z0-9])")
        if rx.search(low):
            hits.append(t)
    return hits


def _strategy_tags(text: str) -> list[str]:
    return _term_hits(text, FILTERS["positive_terms"].keys())


def classify_icp(name: str, fund_type: str) -> dict[str, Any]:
    """Decide whether a Form D fund fits the active-manager ICP.

    Returns {include, score, matched, reason}. Rejection reasons:
    'excluded_type' (PE/VC), 'negative_term' (RE/private-credit/etc.),
    'no_strategy' (require_strategy_match and the name names no strategy).
    """
    text = name or ""
    if fund_type in FILTERS["exclude_fund_types"]:
        return {"include": False, "reason": "excluded_type", "matched": [], "score": 0}

    neg = _term_hits(text, FILTERS["negative_terms"])
    if neg:
        return {"include": False, "reason": "negative_term", "matched": [], "score": 0, "neg": neg}

    is_hedge = fund_type.strip().lower() == "hedge fund"
    matched = _term_hits(text, FILTERS["positive_terms"].keys())
    # "Hedge Fund" type is an active manager by SEC classification, so it stays
    # (ranked low unless a strategy is named). Other types (incl. the noisy
    # "Other Investment Fund") must name an ICP strategy to survive.
    if not matched and not is_hedge and FILTERS.get("require_strategy_match", True):
        return {"include": False, "reason": "no_strategy", "matched": [], "score": 0}

    strat_score = sum(FILTERS["positive_terms"][m] for m in matched)
    base = 50 + (8 if is_hedge else 0)
    return {"include": True, "reason": "icp_fit", "matched": matched, "score": base + strat_score}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ensure_identity() -> str | None:
    identity = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not identity:
        return None
    edgar.set_identity(identity)
    return identity


def _ensure_snapshot_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manager_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cik TEXT NOT NULL, manager_name TEXT, accession TEXT NOT NULL,
            report_period TEXT, total_value REAL, total_holdings INTEGER,
            options_concentration REAL,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (cik, accession)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_cik ON manager_snapshots(cik)")


def _num(value: Any) -> float | None:
    try:
        s = str(value).replace(",", "").replace("$", "").strip()
        if not s or s.lower() in ("indefinite", "none", "n/a"):
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _strategy_tags(text: str) -> list[str]:
    low = (text or "").lower()
    seen: list[str] = []
    for kw in STRATEGY_KEYWORDS:
        if kw in low and kw not in seen:
            seen.append(kw)
    return seen


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= div:
            return f"${value / div:.1f}{unit}"
    return f"${value:,.0f}"


def _edgar_url(cik: str) -> str:
    cik_clean = str(cik).lstrip("0") or "0"
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_clean}&type=&dateb=&owner=include&count=40"


# ---------------------------------------------------------------------------
# Form D — new hedge-fund launches
# ---------------------------------------------------------------------------
def _related_person_names(obj: Any) -> list[str]:
    names: list[str] = []
    for p in (getattr(obj, "related_persons", None) or []):
        n = getattr(p, "name", None) or str(p)
        n = str(n).strip()
        if n and n not in names:
            names.append(n)
    return names[:8]


# --- infrastructure-inflection enrichment -------------------------------
# A $50M+ first raise signals an institutional-scale launch that will face
# operational due diligence (allocators rank investment infrastructure ~78% as
# very/extremely important) — i.e. it needs to scale its operating platform.
LARGE_RAISE_THRESHOLD = 50_000_000
VERY_LARGE_RAISE_THRESHOLD = 250_000_000

_MGR_STOPWORDS = {
    "lp", "llc", "ltd", "inc", "lllp", "gp", "fund", "funds", "master", "offshore",
    "onshore", "domestic", "international", "intl", "cayman", "series", "trust",
    "spc", "sicav", "qp", "the", "a", "of", "co", "company", "vehicle", "feeder",
}


def _manager_key(name: str) -> str:
    """Collapse a fund's legal name onto its manager identity, so master/feeder/
    parallel vehicles of one platform group together (platform-expansion signal)."""
    toks = [t for t in _re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split() if t not in _MGR_STOPWORDS]
    return " ".join(toks[:4])


def _enrich_inflection(cands: list[dict[str, Any]]) -> None:
    """Tag candidates with platform-expansion (same manager filing multiple
    vehicles this window) in place. ``large_raise`` is set per-candidate already."""
    counts: dict[str, int] = {}
    keys = [_manager_key(c.get("fund", "")) for c in cands]
    for k in keys:
        if k:
            counts[k] = counts.get(k, 0) + 1
    for c, k in zip(cands, keys):
        n = counts.get(k, 1)
        c["vehicle_count"] = n
        c["platform_expansion"] = bool(k) and n >= 2


# EDGAR full-text search narrows the daily Form D firehose to the ICP fund
# types server-side, so we fetch ~3.5x fewer documents AND cover the whole
# window (no blunt scan cap silently dropping filings past N).
EFTS_ICP_TYPE_QUERIES = ('"Hedge Fund"', '"Other Investment Fund"')


def _efts_icp_accessions(date_from: str, date_to: str) -> tuple[set[str], bool]:
    """Accessions of Form D whose text names an ICP fund type, via efts.sec.gov.

    Returns (accessions, ok). ok=False means EFTS failed and the caller should
    fall back to scanning everything. EFTS lags ~1 day, so the caller still
    fetches the freshest day directly.
    """
    import gzip
    import urllib.parse
    import urllib.request

    ua = os.environ.get("EDGAR_IDENTITY", "Coremont Clarion Prospecting")
    accs: set[str] = set()
    try:
        for q in EFTS_ICP_TYPE_QUERIES:
            frm = 0
            while True:
                url = (
                    "https://efts.sec.gov/LATEST/search-index?"
                    f"q={urllib.parse.quote(q)}&forms=D&startdt={date_from}&enddt={date_to}&from={frm}"
                )
                raw = urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": ua}), timeout=25
                ).read()
                try:
                    raw = gzip.decompress(raw)
                except Exception:  # noqa: BLE001
                    pass
                data = json.loads(raw)
                hits = data.get("hits", {}).get("hits", [])
                for h in hits:
                    acc = str(h.get("_id", "")).split(":")[0]
                    if acc:
                        accs.add(acc)
                total = data.get("hits", {}).get("total", {}).get("value", 0)
                frm += len(hits)
                if not hits or frm >= total or frm >= 1000:
                    break
        return accs, True
    except Exception:  # noqa: BLE001 - any EFTS failure -> caller falls back
        return accs, False


def scan_form_d_launches(date_from: str, date_to: str, cap: int) -> dict[str, Any]:
    """SEC-FETCH stage (runs where SEC is reachable, e.g. a GitHub runner).

    Returns RAW candidates — pooled investment funds that survive the cheap
    name/type filters — with the issuer detail the verifier needs (related
    persons, address). Verdict application + presentation happen later in
    _split_candidates(), so this stage never needs the verification store and the
    render stage never needs SEC.
    """
    if cap <= 0:
        return {"candidates": [], "scanned": 0, "truncated": False, "total": 0}
    try:
        filings = edgar.get_filings(form="D", filing_date=f"{date_from}:{date_to}")
    except Exception as exc:  # noqa: BLE001
        return {"candidates": [], "scanned": 0, "truncated": False, "total": 0, "error": str(exc)}
    if not filings:
        # EDGAR reachable but returned nothing. A multi-day window should never be
        # truly empty, so flag it as suspect (likely a connectivity/index issue).
        return {"candidates": [], "scanned": 0, "truncated": False, "total": 0, "empty_window": True}

    total = len(filings)
    # EFTS pre-filter: only deep-fetch filings whose text names an ICP fund type,
    # plus the freshest day (EFTS lags ~1 day). Falls back to all on EFTS error.
    efts_accs, efts_ok = _efts_icp_accessions(date_from, date_to)
    eligible = []
    for i in range(total):
        f = filings[i]
        if efts_ok:
            acc = str(getattr(f, "accession_no", ""))
            fdate = str(getattr(f, "filing_date", ""))
            if acc not in efts_accs and fdate < date_to:
                continue
        eligible.append(f)

    candidates: list[dict[str, Any]] = []
    scanned = 0
    rejected: dict[str, int] = {
        "verified_not_target": 0, "excluded_type": 0, "negative_term": 0,
        "no_strategy": 0, "not_pooled": 0,
    }
    rejected_sample: list[str] = []
    for f_item in eligible:
        if scanned >= cap:
            break
        scanned += 1
        try:
            obj = f_item.obj()
        except Exception:  # noqa: BLE001 - skip unparseable
            continue
        if str(getattr(obj, "submission_type", "")).upper() != "D":
            continue  # new notice only, not an amendment
        od = getattr(obj, "offering_data", None)
        ig = getattr(od, "industry_group", None) if od else None
        igt = str(getattr(ig, "industry_group_type", "") or "") if ig else ""
        ifi = getattr(ig, "investment_fund_info", None) if ig else None
        fund_type = str(getattr(ifi, "investment_fund_type", "") or "")

        issuer = getattr(obj, "primary_issuer", None)
        name = str(getattr(issuer, "entity_name", "")) if issuer else ""
        cik = str(getattr(issuer, "cik", "")) if issuer else ""

        # Only pooled investment funds are candidates (drops operating cos / RE).
        if igt.strip().lower() != "pooled investment fund":
            rejected["not_pooled"] += 1
            continue

        # A fund survives the cheap filter if it's already a confirmed target OR
        # it passes the name/type classifier. Confirmed non-targets are dropped
        # here; everything else is decided later from the verdict store.
        ver = VERIFICATIONS.get(_norm_cik(cik)) if cik else None
        if ver is not None and not ver.get("is_target", False):
            rejected["verified_not_target"] += 1
            if len(rejected_sample) < 12:
                rejected_sample.append(f"{name[:48]} [verified: {ver.get('business', 'not a target')[:60]}]")
            continue
        if not (ver and ver.get("is_target")):
            verdict = classify_icp(name, fund_type)
            if not verdict["include"]:
                rejected[verdict["reason"]] = rejected.get(verdict["reason"], 0) + 1
                if len(rejected_sample) < 12:
                    why = verdict.get("neg") or fund_type or verdict["reason"]
                    rejected_sample.append(f"{name[:48]} [{verdict['reason']}: {why}]")
                continue

        osa = getattr(od, "offering_sales_amounts", None)
        sold = _num(getattr(osa, "total_amount_sold", None)) if osa else None
        candidates.append(
            {
                "cik": cik,
                "fund": name,
                "fund_type": fund_type,
                "accession": str(getattr(f_item, "accession_no", "")),
                "filed": str(getattr(f_item, "filing_date", "")),
                "jurisdiction": str(getattr(issuer, "jurisdiction", "")) if issuer else "",
                "address": " ".join(str(getattr(issuer, "primary_address", "")).split()) if issuer else "",
                "related_persons": _related_person_names(obj),
                "amount_sold": sold,
                "total_offering": _num(getattr(osa, "total_offering_amount", None)) if osa else None,
                "first_sale": str(getattr(od, "date_of_first_sale", "")) if od else "",
                "strategy_tags": _term_hits(name, FILTERS["positive_terms"].keys()),
                # Infrastructure-inflection enrichments (see _enrich_inflection).
                "large_raise": bool(sold and sold >= LARGE_RAISE_THRESHOLD),
            }
        )
    _enrich_inflection(candidates)
    return {
        "candidates": candidates,
        "scanned": scanned,
        "truncated": len(eligible) > cap,
        "total": total,
        "eligible": len(eligible),
        "efts_filtered": efts_ok,
        "rejected": rejected,
        "rejected_sample": rejected_sample,
    }


def _score_candidate(c: dict[str, Any], verified: bool) -> int:
    tags = c.get("strategy_tags") or []
    weight = sum(FILTERS["positive_terms"].get(t, 0) for t in tags)
    if verified:
        score = max(58 + weight, FILTERS.get("tiers", {}).get("high", 78))
    else:
        is_hedge = str(c.get("fund_type", "")).strip().lower() == "hedge fund"
        score = 50 + (8 if is_hedge else 0) + weight
    sold = c.get("amount_sold") or 0
    if sold >= VERY_LARGE_RAISE_THRESHOLD:
        score += 20
    elif sold >= LARGE_RAISE_THRESHOLD:
        score += 12
    if c.get("platform_expansion"):
        score += 8
    if c.get("new_registration"):
        score += 12
    if c.get("adv_hedge_confirmed"):
        score += 8
    adv_aum = c.get("adv_aum") or 0
    if adv_aum >= 1e9:
        score += 8
    if c.get("first_13f"):
        score += 25  # crossed the $100M institutional threshold = strong scaler
    return score


def _inflection_labels(c: dict[str, Any]) -> list[str]:
    out = []
    if c.get("new_registration"):
        out.append("newly SEC-registered adviser (first-time platform build)")
    if c.get("large_raise"):
        sold = c.get("amount_sold") or 0
        out.append(f"institutional-scale raise (${sold/1e6:.0f}M)")
    if c.get("platform_expansion"):
        out.append(f"platform expansion ({c.get('vehicle_count')} vehicles this week)")
    if c.get("adv_private_funds") and (c.get("adv_private_funds") or 0) >= 5:
        out.append(f"scaling platform ({int(c['adv_private_funds'])} private funds on ADV)")
    return out


def _adv_facts(c: dict[str, Any]) -> list[str]:
    facts = []
    if c.get("adv_matched"):
        if c.get("adv_aum"):
            facts.append(f"ADV AUM: {_money(c['adv_aum'])}")
        mix = c.get("adv_fund_mix") or {}
        if mix.get("hedge"):
            facts.append(f"{int(mix['hedge'])} hedge fund(s) on ADV")
        if c.get("adv_status"):
            facts.append(f"ADV: {c['adv_status']}")
    return facts


def _candidate_to_signal(c: dict[str, Any], verified: bool, business: str | None) -> dict[str, Any]:
    tags = c.get("strategy_tags") or []
    strat_txt = business or (", ".join(tags) if tags else c.get("fund_type") or "fund")
    sold = c.get("amount_sold")
    infl = _inflection_labels(c)
    is_first_13f = bool(c.get("first_13f"))

    if is_first_13f:
        tv = c.get("total_value")
        sig = "first_13f"
        infl = ["first-ever 13F filing — crossed the $100M institutional threshold"] + infl
        why = (
            f"{c.get('fund', 'This manager')} just filed its FIRST 13F"
            + (f" (~{_money(tv)} in 13(f) securities)" if tv else "")
            + " — it has scaled past the institutional-coverage threshold and "
            "is exactly where operational due diligence on its platform begins."
        )
    else:
        sig = "greenfield_launch"
        why = (
            f"New {c.get('fund_type') or 'fund'} ({strat_txt}) just filed its first Form D"
            + (f", ${sold/1e6:.0f}M raised so far" if sold else "")
            + ". "
        )
        if infl:
            why += "Infrastructure inflection: " + "; ".join(infl) + " — "
        why += "evaluating its operating platform for the first time and must scale to pass operational due diligence."

    return {
        "signal": sig,
        "fund": c.get("fund", ""),
        "cik": c.get("cik", ""),
        "score": _score_candidate(c, verified),
        "verified": verified or None,
        "business": business,
        "fund_type": c.get("fund_type", ""),
        "amount_sold": sold,
        "total_value": c.get("total_value"),
        "total_offering": c.get("total_offering"),
        "first_sale": c.get("first_sale", ""),
        "jurisdiction": c.get("jurisdiction", ""),
        "accession": c.get("accession", ""),
        "filed": c.get("filed", ""),
        "strategy_tags": tags,
        "inflection": infl,
        "adv_matched": c.get("adv_matched"),
        "adv_aum": c.get("adv_aum"),
        "adv_status": c.get("adv_status"),
        "adv_fund_mix": c.get("adv_fund_mix"),
        "adv_private_funds": c.get("adv_private_funds"),
        "new_registration": c.get("new_registration"),
        "why": why,
    }


def split_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the (current) verdict store to raw candidates — NO network.

    Confirmed targets become presented signals; confirmed non-targets are
    dropped; un-verdicted candidates are held in the pending queue (unless
    require_verification is off, in which case they're shown as 'unverified').
    """
    require_verification = FILTERS.get("require_verification", True)
    verdicts = _load_verifications()
    signals: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    dropped = 0
    for c in candidates:
        ver = verdicts.get(_norm_cik(c.get("cik", ""))) if c.get("cik") else None
        if ver is not None and not ver.get("is_target", False):
            dropped += 1
            continue
        if ver is not None and ver.get("is_target"):
            signals.append(_candidate_to_signal(c, True, ver.get("business")))
        elif require_verification:
            pending.append(
                {
                    k: c.get(k)
                    for k in (
                        "cik", "fund", "fund_type", "accession", "address",
                        "related_persons", "adv_matched", "adv_name", "adv_aum",
                        "adv_fund_mix", "adv_hedge_confirmed", "new_registration",
                    )
                }
            )
        else:
            signals.append(_candidate_to_signal(c, False, None))
    return {"signals": signals, "pending": pending, "verified_not_target": dropped}


# ---------------------------------------------------------------------------
# 13F — AUM growth + new derivatives
# ---------------------------------------------------------------------------
def _read_watchlist() -> list[str]:
    path = CONFIG_DIR / "watchlist.txt"
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _collect_13f(date_from: str, date_to: str, cap: int) -> tuple[dict[str, Any], bool, str | None]:
    """Map cik -> Filing for recent 13F-HR + explicit watchlist entries."""
    targets: dict[str, Any] = {}
    truncated = False
    error: str | None = None
    window = None
    if cap > 0:
        try:
            window = edgar.get_filings(form="13F-HR", filing_date=f"{date_from}:{date_to}")
        except Exception as exc:  # noqa: BLE001
            window = None
            error = str(exc)
    if window:
        n = len(window)
        truncated = n > cap
        for i in range(min(n, cap)):
            f = window[i]
            targets.setdefault(str(getattr(f, "cik", "")), f)

    for entry in _read_watchlist():
        try:
            company = edgar.Company(entry)
            latest = company.get_filings(form="13F-HR").latest()
            if latest is not None:
                targets.setdefault(str(getattr(latest, "cik", entry)), latest)
        except Exception:  # noqa: BLE001 - defensive, watchlist entries may not resolve
            continue
    return targets, truncated, error


def _options_concentration(obj: Any) -> float | None:
    xml = getattr(obj, "infotable_xml", None)
    if not xml:
        return None
    parsed = sec_parser.parse_13f_infotable(xml.encode() if isinstance(xml, str) else xml)
    if parsed["status"] != "success":
        return None
    conc = sec_parser.options_concentration(parsed["data"]["holdings"])
    if conc["status"] != "success":
        return None
    return conc["data"]["options_concentration"]


def _icp_13f_ok(c: dict[str, Any]) -> bool:
    """A first-13F filer is an ICP candidate only with private-fund evidence
    (ADV private funds > 0 / hedge confirmed) or a named strategy — otherwise
    it's a wealth manager / RIA crossing $100M in client SMAs, not a hedge fund."""
    return (
        (c.get("adv_private_funds") or 0) > 0
        or bool(c.get("adv_hedge_confirmed"))
        or bool(c.get("strategy_tags"))
    )


def scan_13f_new_filers(date_from: str, date_to: str, cap: int) -> dict[str, Any]:
    """Find FIRST-EVER 13F-HR filers in the window — a manager that just crossed
    $100M in 13(f) securities (institutional-scale inflection / scaler).

    Returns candidate dicts (joined to the Form D candidate pool) so they get the
    same classify -> ADV-enrich -> verify -> render treatment. SEC-fetch stage.
    """
    if cap <= 0:
        return {"candidates": [], "checked": 0}
    try:
        filings = edgar.get_filings(form="13F-HR", filing_date=f"{date_from}:{date_to}")
    except Exception as exc:  # noqa: BLE001
        return {"candidates": [], "checked": 0, "error": str(exc)}
    if not filings:
        return {"candidates": [], "checked": 0}

    candidates: list[dict[str, Any]] = []
    seen_cik: set[str] = set()
    checked = 0
    for i in range(len(filings)):
        if checked >= cap:
            break
        f = filings[i]
        cik = str(getattr(f, "cik", ""))
        if not cik or cik in seen_cik:
            continue
        seen_cik.add(cik)
        checked += 1
        # First-ever 13F-HR? (the entity has exactly one in its EDGAR history)
        try:
            hist = edgar.Company(cik).get_filings(form="13F-HR")
            if hist is None or len(hist) != 1:
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            obj = f.obj()
        except Exception:  # noqa: BLE001
            continue
        name = str(getattr(obj, "management_company_name", "") or getattr(f, "company", ""))
        # Drop obvious non-ICP 13F filers (banks, insurers, pensions) cheaply.
        if _term_hits(name, FILTERS["negative_terms"] + ["bank", "insurance", "pension", "trust company", "advisors inc"]):
            continue
        candidates.append(
            {
                "cik": cik,
                "fund": name,
                "fund_type": "",
                "first_13f": True,
                "total_value": _num(getattr(obj, "total_value", None)),
                "accession": str(getattr(f, "accession_no", "")),
                "filed": str(getattr(f, "filing_date", "")),
                "related_persons": [],
                "strategy_tags": _term_hits(name, FILTERS["positive_terms"].keys()),
            }
        )
    return {"candidates": candidates, "checked": checked}


def scan_13f_moves(conn: sqlite3.Connection, date_from: str, date_to: str, cap: int) -> dict[str, Any]:
    targets, truncated, error = _collect_13f(date_from, date_to, cap)
    signals: list[dict[str, Any]] = []
    baselined = 0

    for cik, filing in targets.items():
        try:
            obj = filing.obj()
        except Exception:  # noqa: BLE001
            continue
        accession = str(getattr(filing, "accession_no", ""))
        period = str(getattr(obj, "report_period", ""))
        # edgartools returns total_value as a Decimal — coerce so sqlite3 can bind
        # it and so arithmetic with prior float snapshots works.
        total_value = _num(getattr(obj, "total_value", None))
        _th = getattr(obj, "total_holdings", None)
        total_holdings = int(_th) if _th is not None else None
        name = str(getattr(obj, "management_company_name", "") or getattr(filing, "company", ""))

        # prior snapshot (older report period) for this manager, before we insert
        prior = conn.execute(
            "SELECT total_value, options_concentration, report_period FROM manager_snapshots "
            "WHERE cik = ? AND report_period < ? ORDER BY report_period DESC LIMIT 1",
            (cik, period),
        ).fetchone()

        # only pay for the options parse when we can diff or it's worth recording
        conc = _options_concentration(obj)

        conn.execute(
            "INSERT OR IGNORE INTO manager_snapshots "
            "(cik, manager_name, accession, report_period, total_value, total_holdings, options_concentration) "
            "VALUES (?,?,?,?,?,?,?)",
            (cik, name, accession, period, total_value, total_holdings, conc),
        )

        if not prior:
            baselined += 1
            continue

        prior_tv, prior_conc, prior_period = prior
        # AUM growth
        if prior_tv and total_value and prior_tv > 0:
            chg = (total_value - prior_tv) / prior_tv
            if chg >= AUM_GROWTH_THRESHOLD:
                signals.append(
                    {
                        "signal": "aum_growth",
                        "fund": name,
                        "cik": cik,
                        "score": min(95, 55 + int(chg * 100)),
                        "pct_change": round(chg * 100, 1),
                        "total_value": total_value,
                        "prior_value": prior_tv,
                        "period": period,
                        "prior_period": prior_period,
                        "accession": accession,
                        "strategy_tags": _strategy_tags(name),
                        "why": f"13F book grew {chg*100:.0f}% ({_money(prior_tv)} → {_money(total_value)}) "
                        f"vs {prior_period}. Rapid growth is where operational scaling pain appears.",
                    }
                )
        # new / rising derivatives
        if conc is not None and conc > OPTIONS_THRESHOLD:
            rose = prior_conc is None or conc - prior_conc >= 0.05 or prior_conc <= OPTIONS_THRESHOLD
            if rose:
                signals.append(
                    {
                        "signal": "derivatives_complex",
                        "fund": name,
                        "cik": cik,
                        "score": min(95, 55 + int((conc - OPTIONS_THRESHOLD) * 100)),
                        "options_concentration": round(conc, 3),
                        "prior_concentration": round(prior_conc, 3) if prior_conc is not None else None,
                        "period": period,
                        "accession": accession,
                        "strategy_tags": _strategy_tags(name),
                        "why": f"Options now {conc*100:.0f}% of the 13F book"
                        + (f" (up from {prior_conc*100:.0f}%)" if prior_conc is not None else "")
                        + " — rising derivatives = new valuation/risk complexity.",
                    }
                )
    conn.commit()
    return {"signals": signals, "tracked": len(targets), "baselined": baselined, "truncated": truncated, "error": error}


# ---------------------------------------------------------------------------
# persistence + ranking
# ---------------------------------------------------------------------------
def _tier(score: int) -> str:
    t = FILTERS.get("tiers", {})
    if score >= t.get("high", 78):
        return "High"
    if score >= t.get("medium", 60):
        return "Medium"
    return "Watch"


def _persist(signal: dict[str, Any]) -> None:
    cik = signal.get("cik") or ""
    if not cik:
        return
    crd = f"CIK{cik}"
    strategies = ", ".join(signal.get("strategy_tags") or []) or signal.get("fund_type", "") or signal["signal"]
    tier = _tier(signal["score"])
    status = "QUALIFIED" if tier in ("High", "Medium") else "RAW"
    res = upsert_entity(crd, cik, signal.get("fund", "")[:200] or "Unknown", strategies, status="RAW")
    if res["status"] == "success":
        eid = res["data"]["entity_id"]
        add_observation(eid, signal["signal"], signal.get("why", "")[:500], signal["signal"])
        if status == "QUALIFIED":
            set_status(crd, "QUALIFIED")


def run_brief(
    *,
    date_from_d: str,
    date_to: str,
    date_from_13f: str,
    formd_cap: int,
    cap_13f: int,
    cap_new13f: int = 0,
    from_candidates: str | None = None,
) -> dict[str, Any]:
    """Produce the brief. Two stages, split so the cloud never touches SEC:

    - SCAN (default): fetch SEC (Form D + 13F), write briefs/candidates.json.
      Run where SEC is reachable (a GitHub runner, or locally).
    - RENDER (from_candidates=<path>): read candidates.json, apply current
      verdicts, render. No network — safe in the SEC-blocked cloud sandbox.
    """
    ensure_dirs()
    if not DB_PATH.exists():
        return fatal("memory.db not found; run tools/init_memory_db.py first")
    candidates_path = BRIEFS_DIR / "candidates.json"
    scan_errors: list[str] = []

    if from_candidates:
        path = Path(from_candidates)
        if not path.exists():
            return fatal(f"candidates file not found: {path}")
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return fatal(f"could not read candidates file: {exc}")
        candidates = cached.get("candidates", [])
        moves_signals = cached.get("moves", [])
        scan_meta = cached.get("scan", {})
        cov13 = cached.get("coverage_13f", {})
        window = cached.get("window", {"form_d": "(cached)", "thirteenf": "(cached)"})
        source_label = f"cached candidates from {cached.get('generated_at', '?')} (no SEC fetch)"
    else:
        if not _ensure_identity():
            return fatal("EDGAR_IDENTITY is not set; cannot scan EDGAR")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_snapshot_table(conn)
        launches = scan_form_d_launches(date_from_d, date_to, formd_cap)
        new13f = scan_13f_new_filers(date_from_13f, date_to, cap_new13f)
        moves = scan_13f_moves(conn, date_from_13f, date_to, cap_13f)
        conn.close()
        candidates = launches.get("candidates", []) + new13f.get("candidates", [])
        # Form ADV enrichment (adviser AUM, hedge-fund confirmation, new
        # registration). Annotates candidates in place; never fatal.
        adv_cov = adv_enrich.enrich(candidates)
        # Tighten the 13F-first stream: most new 13F filers are wealth managers /
        # RIAs crossing $100M in client SMAs, not hedge funds. A real hedge-fund
        # manager runs private funds (ADV) or names a strategy; drop the rest.
        before = len(candidates)
        candidates = [c for c in candidates if not (c.get("first_13f") and not _icp_13f_ok(c))]
        new13f_dropped = before - len(candidates)
        moves_signals = moves.get("signals", [])
        scan_meta = {
            "scanned": launches.get("scanned", 0),
            "total": launches.get("total", 0),
            "eligible": launches.get("eligible", launches.get("scanned", 0)),
            "efts_filtered": launches.get("efts_filtered", False),
            "truncated": launches.get("truncated", False),
            "rejected": launches.get("rejected", {}),
            "rejected_sample": launches.get("rejected_sample", []),
            "adv": adv_cov,
            "new_13f_filers": sum(1 for c in candidates if c.get("first_13f")),
            "new_13f_checked": new13f.get("checked", 0),
            "new_13f_dropped_noise": new13f_dropped,
        }
        cov13 = {
            "tracked": moves.get("tracked", 0),
            "baselined": moves.get("baselined", 0),
            "truncated": moves.get("truncated", False),
        }
        window = {"form_d": f"{date_from_d} → {date_to}", "thirteenf": f"{date_from_13f} → {date_to}"}
        source_label = "live SEC scan"
        if launches.get("error"):
            scan_errors.append(f"Form D scan failed: {launches['error']}")
        elif launches.get("empty_window") and formd_cap > 0:
            scan_errors.append(
                f"Form D scan returned 0 filings for {date_from_d}→{date_to} "
                "(a multi-day window is never truly empty — likely SEC was unreachable)."
            )
        if moves.get("error"):
            scan_errors.append(f"13F scan failed: {moves['error']}")
        # Commit-able hand-off artifact for the render stage.
        candidates_path.write_text(
            json.dumps(
                {
                    "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "window": window,
                    "candidates": candidates,
                    "moves": moves_signals,
                    "scan": scan_meta,
                    "coverage_13f": cov13,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    # Apply current verdicts (no network) and assemble the presented brief.
    split = split_candidates(candidates)
    pending = split["pending"]
    launch_signals = split["signals"]
    signals = launch_signals + moves_signals
    for s in signals:
        s["tier"] = _tier(s["score"])
        _persist(s)
    signals.sort(key=lambda s: s["score"], reverse=True)

    rejected = dict(scan_meta.get("rejected", {}))
    rejected["verified_not_target"] = rejected.get("verified_not_target", 0) + split["verified_not_target"]

    meta = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": source_label,
        "window": window,
        "counts": {
            "total": len(signals),
            "launches": len(launch_signals),
            "aum_growth": sum(1 for s in moves_signals if s["signal"] == "aum_growth"),
            "derivatives": sum(1 for s in moves_signals if s["signal"] == "derivatives_complex"),
            "high": sum(1 for s in signals if s["tier"] == "High"),
            "pending_verification": len(pending),
        },
        "coverage": {
            "form_d_scanned": scan_meta.get("scanned", 0),
            "form_d_kept": len(launch_signals),
            "form_d_filtered": rejected,
            "form_d_rejected_sample": scan_meta.get("rejected_sample", []),
            "form_d_truncated": scan_meta.get("truncated", False),
            "managers_tracked": cov13.get("tracked", 0),
            "managers_baselined": cov13.get("baselined", 0),
            "thirteenf_truncated": cov13.get("truncated", False),
        },
        "scan_errors": scan_errors,
        "scan_ok": not scan_errors,
    }

    html_path = render_html(signals, meta, pending)
    json_path = BRIEFS_DIR / "latest.json"
    json_path.write_text(
        json.dumps({"meta": meta, "signals": signals, "pending": pending}, indent=2, default=str),
        encoding="utf-8",
    )
    status = "retry" if scan_errors else "success"
    log_execution("morning_brief", "run", status, json.dumps(meta["counts"]))

    payload = {
        "html": str(html_path),
        "json": str(json_path),
        "candidates": str(candidates_path),
        "pending": pending,
        **meta,
    }
    if scan_errors:
        return retry("; ".join(scan_errors), data=payload)
    return ok(payload)


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------
SIGNAL_LABEL = {
    "greenfield_launch": "New fund launch",
    "first_13f": "First 13F (institutional scale)",
    "aum_growth": "AUM growth",
    "derivatives_complex": "New derivatives",
}
SIGNAL_COLOR = {
    "greenfield_launch": "#2563eb",
    "first_13f": "#0891b2",
    "aum_growth": "#16a34a",
    "derivatives_complex": "#9333ea",
}
TIER_COLOR = {"High": "#dc2626", "Medium": "#d97706", "Watch": "#64748b"}


def _short_hook(signal_key: str) -> str:
    raw = COPY_BLOCKS.get(signal_key, "")
    return raw.replace("**", "")


def _card(s: dict[str, Any]) -> str:
    fund = html.escape(s.get("fund", "Unknown"))
    cik = html.escape(str(s.get("cik", "")))
    sig = s["signal"]
    label = SIGNAL_LABEL.get(sig, sig)
    color = SIGNAL_COLOR.get(sig, "#475569")
    tier = s.get("tier", "Watch")
    tcolor = TIER_COLOR.get(tier, "#64748b")
    why = html.escape(s.get("why", ""))
    tags = " ".join(f"<span class='tag'>{html.escape(t)}</span>" for t in (s.get("strategy_tags") or []))

    facts = []
    if s.get("amount_sold"):
        facts.append(f"Raised so far: {_money(s['amount_sold'])}")
    if s.get("total_value"):
        facts.append(f"13F book: {_money(s['total_value'])}")
    if s.get("pct_change"):
        facts.append(f"QoQ: +{s['pct_change']}%")
    if s.get("options_concentration") is not None:
        facts.append(f"Options: {s['options_concentration']*100:.0f}% of book")
    if s.get("fund_type"):
        facts.append(html.escape(s["fund_type"]))
    if s.get("first_sale"):
        facts.append(f"First sale: {html.escape(s['first_sale'])}")
    facts += [html.escape(x) for x in _adv_facts(s)]
    facts_html = " &middot; ".join(facts)

    hook = html.escape(_short_hook(sig))
    url = _edgar_url(cik)
    draft_cmd = f"py tools/sales_copilot.py --crd CIK{cik}" if cik else ""

    verified = s.get("verified")
    if verified is True:
        vbadge = '<span class="vchip ok">&#10003; Verified</span>'
    elif verified is None and sig == "greenfield_launch":
        vbadge = '<span class="vchip warn">Unverified</span>'
    else:
        vbadge = ""
    business = s.get("business")
    business_html = f'<div class="biz">{html.escape(business)}</div>' if business else ""
    infl = s.get("inflection") or []
    infl_html = (
        '<div class="infl">' + " ".join(f"<span>&#9889; {html.escape(x)}</span>" for x in infl) + "</div>"
        if infl else ""
    )

    return f"""
    <div class="card" style="border-left-color:{color}">
      <div class="card-top">
        <span class="tier" style="background:{tcolor}">{tier}</span>
        <span class="sig" style="color:{color}">{label}</span>
        {vbadge}
        <span class="score">{s['score']}</span>
      </div>
      <div class="fund">{fund}</div>
      {business_html}
      {infl_html}
      <div class="why">{why}</div>
      {f'<div class="facts">{facts_html}</div>' if facts_html else ''}
      {f'<div class="tags">{tags}</div>' if tags else ''}
      <div class="hook"><strong>Clarion angle:</strong> {hook}</div>
      <div class="actions">
        <a href="{url}" target="_blank">View on EDGAR &rarr;</a>
        {f'<code>{html.escape(draft_cmd)}</code>' if draft_cmd else ''}
      </div>
    </div>"""


def render_html(signals: list[dict[str, Any]], meta: dict[str, Any], pending: list | None = None) -> Path:
    ensure_dirs()
    pending = pending or []
    c = meta["counts"]
    cov = meta["coverage"]
    cards = "\n".join(_card(s) for s in signals) or (
        "<div class='empty'>No <em>verified</em> signals in this window yet."
        + (f" {len(pending)} candidate(s) are awaiting verification." if pending else "")
        + " 13F-driven signals (AUM growth / derivatives) cluster around quarterly "
        "filing deadlines (mid-Feb / May / Aug / Nov).</div>"
    )
    error_note = ""
    scan_errors = meta.get("scan_errors") or []
    if scan_errors:
        items = "".join(f"<li>{html.escape(e)}</li>" for e in scan_errors)
        error_note = (
            "<div class='note err'><strong>&#9888; Scan incomplete — this brief may be missing funds.</strong>"
            f"<ul style='margin:6px 0 0 18px'>{items}</ul></div>"
        )

    pending_note = ""
    if pending:
        pending_note = (
            f"<div class='note'>{len(pending)} new candidate(s) are being verified and "
            "are intentionally not shown until confirmed to be real trading funds. "
            "Everything below is verified.</div>"
        )
    filt = cov.get("form_d_filtered", {}) or {}
    filtered_total = sum(filt.values())
    filter_note = ""
    if filtered_total:
        parts = []
        labels = {
            "verified_not_target": "verified non-target (RE / lender / PE)",
            "excluded_type": "PE/VC type",
            "negative_term": "RE / private-credit / etc.",
            "no_strategy": "no active strategy named",
            "not_pooled": "not a pooled fund",
        }
        for k, v in sorted(filt.items(), key=lambda kv: -kv[1]):
            if v:
                parts.append(f"{v} {labels.get(k, k)}")
        filter_note = (
            f"<div class='note ok'>✓ Filtered {filtered_total} non-ICP Form D vehicles "
            f"({'; '.join(parts)}) to keep only active-management strategies.</div>"
        )

    trunc_note = ""
    if cov.get("form_d_truncated") or cov.get("thirteenf_truncated"):
        trunc_note = (
            "<div class='note'>⚠ Coverage capped this run "
            f"(Form D scanned {cov['form_d_scanned']}"
            f"{', list truncated' if cov['form_d_truncated'] else ''}; "
            f"{'13F list truncated' if cov['thirteenf_truncated'] else '13F within cap'}). "
            "Raise --formd-cap / --cap-13f to widen.</div>"
        )

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Coremont Clarion — Morning Brief {meta['generated_at']}</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
         background:#f1f5f9; color:#0f172a; }}
  header {{ background:#0f172a; color:#fff; padding:24px 28px; }}
  header h1 {{ margin:0; font-size:20px; letter-spacing:.2px; }}
  header .sub {{ color:#94a3b8; font-size:13px; margin-top:6px; }}
  .summary {{ display:flex; gap:10px; flex-wrap:wrap; padding:18px 28px; }}
  .stat {{ background:#fff; border:1px solid #e2e8f0; border-radius:10px; padding:12px 16px; min-width:96px; }}
  .stat .n {{ font-size:24px; font-weight:700; }}
  .stat .l {{ font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:#64748b; }}
  .wrap {{ padding:6px 28px 40px; max-width:980px; }}
  .note {{ background:#fef9c3; border:1px solid #fde047; color:#713f12; padding:10px 14px;
          border-radius:8px; font-size:13px; margin:8px 0 16px; }}
  .note.ok {{ background:#dcfce7; border-color:#86efac; color:#14532d; }}
  .note.err {{ background:#fee2e2; border-color:#fca5a5; color:#7f1d1d; }}
  .card {{ background:#fff; border:1px solid #e2e8f0; border-left-width:5px; border-radius:10px;
          padding:16px 18px; margin:12px 0; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  .card-top {{ display:flex; align-items:center; gap:10px; }}
  .tier {{ color:#fff; font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; text-transform:uppercase; }}
  .sig {{ font-weight:600; font-size:13px; }}
  .vchip {{ font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; }}
  .vchip.ok {{ background:#dcfce7; color:#14532d; }}
  .vchip.warn {{ background:#f1f5f9; color:#64748b; border:1px dashed #cbd5e1; }}
  .score {{ margin-left:auto; font-size:12px; color:#94a3b8; }}
  .fund {{ font-size:17px; font-weight:700; margin:8px 0 4px; }}
  .biz {{ font-size:12px; color:#16a34a; font-weight:600; margin:-2px 0 6px; }}
  .infl {{ margin:2px 0 4px; }}
  .infl span {{ display:inline-block; background:#fff7ed; color:#9a3412; border:1px solid #fed7aa;
         font-size:11px; font-weight:600; padding:2px 8px; border-radius:999px; margin:0 4px 4px 0; }}
  .why {{ font-size:14px; color:#334155; }}
  .facts {{ font-size:12px; color:#475569; margin-top:8px; }}
  .tags {{ margin-top:8px; }}
  .tag {{ display:inline-block; background:#eef2ff; color:#3730a3; font-size:11px;
         padding:2px 8px; border-radius:999px; margin:0 4px 4px 0; }}
  .hook {{ font-size:13px; color:#0f172a; background:#f8fafc; border:1px solid #e2e8f0;
          border-radius:8px; padding:10px 12px; margin-top:10px; }}
  .actions {{ margin-top:10px; display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
  .actions a {{ color:#2563eb; text-decoration:none; font-size:13px; font-weight:600; }}
  .actions code {{ background:#0f172a; color:#e2e8f0; font-size:11px; padding:3px 8px; border-radius:6px; }}
  .empty {{ background:#fff; border:1px dashed #cbd5e1; border-radius:10px; padding:28px;
           text-align:center; color:#64748b; }}
  footer {{ padding:18px 28px 40px; color:#94a3b8; font-size:12px; max-width:980px; }}
</style></head>
<body>
  <header>
    <h1>Coremont Clarion — Morning Brief</h1>
    <div class="sub">Generated {meta['generated_at']} &middot; <strong>verified funds only</strong> &middot;
      Form D {meta['window']['form_d']} &middot; 13F {meta['window']['thirteenf']} &middot;
      {cov['managers_tracked']} managers tracked ({cov['managers_baselined']} new baselines)</div>
  </header>
  <div class="summary">
    <div class="stat"><div class="n">{c['total']}</div><div class="l">Verified signals</div></div>
    <div class="stat"><div class="n">{c['high']}</div><div class="l">High priority</div></div>
    <div class="stat"><div class="n">{c['launches']}</div><div class="l">New launches</div></div>
    <div class="stat"><div class="n">{c['aum_growth']}</div><div class="l">AUM growth</div></div>
    <div class="stat"><div class="n">{c['derivatives']}</div><div class="l">New derivatives</div></div>
    <div class="stat"><div class="n">{c.get('pending_verification', 0)}</div><div class="l">Pending verify</div></div>
  </div>
  <div class="wrap">
    {error_note}
    {pending_note}
    {filter_note}
    {trunc_note}
    {cards}
  </div>
  <footer>
    Signals from public SEC EDGAR (Form D, 13F-HR) via edgartools. Drafts require
    human review before any outreach (Tier-4). Run
    <code>py tools/sales_copilot.py --crd CIK&lt;cik&gt;</code> to draft outreach.
  </footer>
</body></html>"""

    today = dt.date.today().isoformat()
    (BRIEFS_DIR / f"brief_{today}.html").write_text(doc, encoding="utf-8")
    latest = BRIEFS_DIR / "latest.html"
    latest.write_text(doc, encoding="utf-8")
    return latest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Coremont Clarion morning brief / dashboard")
    p.add_argument("--days", type=int, default=DEFAULT_FORMD_LOOKBACK, help="Form D lookback (days)")
    p.add_argument("--days-13f", type=int, default=DEFAULT_13F_LOOKBACK, help="13F lookback (days)")
    p.add_argument("--from", dest="date_from", default=None, help="override start YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", default=None, help="override end YYYY-MM-DD")
    p.add_argument("--formd-cap", type=int, default=DEFAULT_FORMD_CAP)
    p.add_argument("--cap-13f", type=int, default=DEFAULT_13F_CAP)
    p.add_argument(
        "--cap-new13f",
        type=int,
        default=0,
        help="scan up to N recent 13F-HR filings for FIRST-time filers (scaler signal)",
    )
    p.add_argument(
        "--from-candidates",
        dest="from_candidates",
        default=None,
        metavar="PATH",
        help="RENDER stage: build the brief from a candidates.json (no SEC fetch). "
        "Use briefs/candidates.json produced by an earlier SEC scan.",
    )
    return p


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = _build_parser().parse_args(argv)
    today = dt.date.today()
    date_to = args.date_to or today.isoformat()
    date_from_d = args.date_from or (today - dt.timedelta(days=args.days)).isoformat()
    date_from_13f = args.date_from or (today - dt.timedelta(days=args.days_13f)).isoformat()
    return run_brief(
        date_from_d=date_from_d,
        date_to=date_to,
        date_from_13f=date_from_13f,
        formd_cap=args.formd_cap,
        cap_13f=args.cap_13f,
        cap_new13f=args.cap_new13f,
        from_candidates=args.from_candidates,
    )


if __name__ == "__main__":
    run_cli(main())
