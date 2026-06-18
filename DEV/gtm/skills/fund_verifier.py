"""fund_verifier — confirm a detected fund is actually a hedge fund.

Runs after any Form D / ADV / 13F detection, BEFORE outreach effort is spent.
We are going after hedge funds specifically: L/S equity qualifies (network
play); macro/RV/credit/multi-strat qualify (Clarion play); real estate,
private credit/capital, PE, and VC are rejected.

Checks, in order:
  1. The shared human/agent verdict store (config/verifications.json, keyed
     by CIK) — authoritative, beats everything, compounds across systems.
  2. IAPD/SEC adviser registration (via the ADV roster) — proves a real,
     registered investment entity.
  3. Web + LinkedIn scraping (Tavily): the fund's own descriptions, scored
     against positive (hedge fund) and negative (real estate / private
     credit / PE / VC) lexicons.

Verdict lands in funds.metadata.verification; high-confidence verdicts are
written back to verifications.json (verified_by="gtm_auto") so the WAT v2
morning brief benefits too. Cached: a verified fund is never re-checked
unless force=True. Unverifiable funds are flagged, not dropped — the
orchestrated drafting session is the human-grade backstop.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from gtm.skills._shared.context import SkillContext, SkillResult

SKILL_NAME = "fund_verifier"

REPO_ROOT = Path(__file__).resolve().parents[2]


def _store_path(cfg: dict[str, Any]) -> Path:
    return REPO_ROOT / str(cfg.get("verifications_json_path", "config/verifications.json"))


def _human_verdict(cfg: dict[str, Any], cik: str | None) -> dict[str, Any] | None:
    if not cik:
        return None
    path = _store_path(cfg)
    if not path.exists():
        return None
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return store.get(str(cik).lstrip("0"))


def _write_back(cfg: dict[str, Any], cik: str | None, verdict: dict[str, Any]) -> bool:
    """Append a high-confidence auto-verdict to the shared store. Never
    overwrites an existing entry (human verdicts stay authoritative)."""
    if not cik or verdict["confidence"] < float(cfg.get("write_back_min_confidence", 0.75)):
        return False
    path = _store_path(cfg)
    try:
        store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        key = str(cik).lstrip("0")
        if key in store:
            return False
        store[key] = {
            "is_target": verdict["is_hedge_fund"],
            "business": verdict["business"],
            "verified_by": "gtm_auto",
            "date": datetime.now(timezone.utc).date().isoformat(),
        }
        path.write_text(json.dumps(store, indent=2), encoding="utf-8")
        return True
    except (OSError, json.JSONDecodeError):
        return False


def score_evidence(texts: list[str], cfg: dict[str, Any]) -> dict[str, Any]:
    """Pure lexicon scoring over gathered source text."""
    weights = cfg.get("weights", {})
    haystack = " ".join(texts).lower()

    positives = sorted({t for t in cfg.get("positive_terms", []) if t.lower() in haystack})
    negatives_by_class: dict[str, list[str]] = {}
    for klass, terms in (cfg.get("negative_terms") or {}).items():
        hits = sorted({t for t in terms if t.lower() in haystack})
        if hits:
            negatives_by_class[klass] = hits

    pos_score = len(positives) * float(weights.get("positive_term", 0.15))
    neg_score = sum(len(v) for v in negatives_by_class.values()) * float(
        weights.get("negative_term", 0.20)
    )
    hints = sorted({
        key
        for key, terms in (cfg.get("strategy_hints") or {}).items()
        if any(t.lower() in haystack for t in terms)
    })
    return {
        "positives": positives,
        "negatives_by_class": negatives_by_class,
        "pos_score": pos_score,
        "neg_score": neg_score,
        "strategy_hints": hints,
    }


def run(ctx: SkillContext, fund_id: str, force: bool = False) -> SkillResult:
    fund = ctx.db.funds.get(UUID(str(fund_id)))
    if fund is None:
        ctx.result.error("resolve", f"fund {fund_id} not found")
        return ctx.result.build()
    ctx.result.records_processed = 1
    cfg = ctx.config

    cached = fund.metadata.get("verification")
    if cached and not force:
        return ctx.result.build(cached=True, **{k: cached[k] for k in ("is_hedge_fund", "business", "confidence")})

    # ── 1. authoritative human/agent verdict ─────────────────────────────────
    human = _human_verdict(cfg, fund.cik)
    if human is not None:
        verdict = {
            "is_hedge_fund": bool(human.get("is_target")),
            "business": human.get("business", ""),
            "confidence": 1.0,
            "method": f"verifications.json ({human.get('verified_by', 'human')})",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if not ctx.dry_run:
            ctx.db.funds.update_metadata(fund.id, {"verification": verdict})
            ctx.result.records_updated = 1
        ctx.logger.info("human_verdict", fund=fund.legal_name, target=verdict["is_hedge_fund"])
        return ctx.result.build(**{k: verdict[k] for k in ("is_hedge_fund", "business", "confidence")})

    # ── 2. registered-entity check (IAPD via ADV roster) ─────────────────────
    evidence_score = 0.0
    sources: list[str] = []
    texts: list[str] = []
    registered = False
    edgar = getattr(ctx.sources, "edgar", None)
    if edgar is not None:
        try:
            profile = edgar.adv_firm_profile(crd=fund.crd, cik=fund.cik, name=fund.legal_name)
            if profile is not None:
                registered = True
                evidence_score += float(cfg.get("weights", {}).get("iapd_registered", 0.25))
                texts.append(profile.firm_name)
                sources.append(f"iapd:crd={profile.crd}")
        except Exception as exc:
            ctx.result.error("iapd", exc)

    # ── 3. the fund's OWN NAME is evidence (often the loudest) ───────────────
    # "X Private Equity Fund", "Y Sale Leaseback I" — self-classification in
    # the legal name carries extra weight in both directions.
    name_scored = score_evidence([fund.legal_name], cfg)
    name_weight = float(cfg.get("weights", {}).get("name_term", 0.35))
    name_neg = sum(len(v) for v in name_scored["negatives_by_class"].values()) * name_weight
    name_pos = len(name_scored["positives"]) * name_weight

    # ── 4. web + LinkedIn scraping ───────────────────────────────────────────
    # Search/match on a short distinctive token — full legal names ("...Fund
    # XIV AIV A1 NY LLC") almost never appear verbatim on the web.
    web = getattr(ctx.sources, "web", None)
    linkedin_hit = False
    if web is not None:
        clean = (fund.common_name or fund.legal_name)
        for noise in (" LP", " L.P.", " LLC", " L.L.C.", " Fund", " Ltd", ","):
            clean = clean.replace(noise, " ")
        token = " ".join(clean.split()[:3]).strip()
        for template in cfg.get("search_queries", []):
            query = template.format(name=token)
            try:
                results = web.search(query, max_results=int(cfg.get("max_results_per_query", 5)))
            except Exception as exc:
                ctx.result.error("search", exc, query=query)
                continue
            for hit in results:
                text = f"{hit.title}. {hit.content}"
                if token.lower() not in text.lower():
                    continue
                texts.append(text)
                sources.append(hit.url)
                if "linkedin.com" in hit.url.lower():
                    linkedin_hit = True
    if linkedin_hit:
        evidence_score += float(cfg.get("weights", {}).get("linkedin_hit", 0.10))

    scored = score_evidence(texts, cfg)
    # fold name evidence into the totals (kept out of `texts` to avoid double count)
    for klass, hits in name_scored["negatives_by_class"].items():
        scored["negatives_by_class"].setdefault(klass, []).extend(
            [h for h in hits if h not in scored["negatives_by_class"].get(klass, [])]
        )
    scored["positives"] = sorted(set(scored["positives"]) | set(name_scored["positives"]))
    scored["strategy_hints"] = sorted(set(scored["strategy_hints"]) | set(name_scored["strategy_hints"]))
    texts = texts or ([fund.legal_name] if (name_neg or name_pos) else texts)

    net_positive = evidence_score + scored["pos_score"] + name_pos - scored["neg_score"] - name_neg
    net_negative = scored["neg_score"] + name_neg - scored["pos_score"] - name_pos

    decision = cfg.get("decision", {})
    if texts and net_negative >= float(decision.get("reject_min", 0.40)):
        dominant = max(scored["negatives_by_class"], key=lambda k: len(scored["negatives_by_class"][k]))
        verdict_value, business = False, (
            f"{dominant.replace('_', ' ')} vehicle (terms: "
            f"{', '.join(scored['negatives_by_class'][dominant][:4])}) - NOT a securities hedge fund"
        )
        confidence = min(0.5 + net_negative, 1.0)
    elif texts and net_positive >= float(decision.get("hedge_fund_min", 0.45)):
        verdict_value, business = True, (
            "Hedge fund (evidence: "
            + ", ".join((scored["positives"] or ["registered adviser"])[:4])
            + (f"; strategy hints: {', '.join(scored['strategy_hints'])}" if scored["strategy_hints"] else "")
            + ")"
        )
        confidence = min(0.5 + net_positive, 1.0)
    else:
        verdict_value, business, confidence = None, "unverified - needs review", 0.0

    verdict = {
        "is_hedge_fund": verdict_value,
        "business": business,
        "confidence": round(confidence, 2),
        "method": "auto (iapd + web + linkedin lexicon)",
        "iapd_registered": registered,
        "positives": scored["positives"],
        "negatives": scored["negatives_by_class"],
        "strategy_hints": scored["strategy_hints"],
        "sources": sources[:8],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if not ctx.dry_run:
        ctx.db.funds.update_metadata(fund.id, {"verification": verdict})
        ctx.result.records_updated = 1
        if verdict_value is not None and _write_back(cfg, fund.cik, verdict):
            ctx.logger.info("verdict_written_to_shared_store", cik=fund.cik)

    ctx.logger.info(
        "verified", fund=fund.legal_name, is_hedge_fund=verdict_value,
        confidence=verdict["confidence"], business=business[:80],
    )
    return ctx.result.build(
        is_hedge_fund=verdict_value, business=business,
        confidence=verdict["confidence"], strategy_hints=scored["strategy_hints"],
    )
