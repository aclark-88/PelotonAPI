"""Daily 6am ET sweep: discovery -> scoring -> contact resolution -> draft queue.

MISSION: find new hedge fund launches and connect with them on LinkedIn —
Clarion PMS angle (risk aggregation, scale, ODD readiness, real-time
multi-asset P&L) for multi-asset/macro/credit launches; network angle
(cap intro, outsourced back office, introductions) for L/S equity launches.

Chain: form_d_sweep -> spinout_watcher -> people_move_detector ->
adv_fit_scorer (new funds) -> apollo_contact_resolver (EVERY launch with a
domain — tier informs urgency, not inclusion) -> DRAFT QUEUE (not
auto-drafting: generation belongs to an orchestrated Claude Code session per
the InjectedLLM convention; the digest lists which person+signal pairs need
drafts and which angle applies).

Each stage is fail-isolated: one stage erroring is reported in the digest and
Slack, the rest still run. Digest lands at gtm/briefs/digest_<date>.md by
06:45 ET and posts to Slack when configured.

    py -m gtm.orchestrator.daily_morning_sweep [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from gtm.orchestrator._sources import build_sources, notify
from gtm.skills import (
    adv_fit_scorer,
    apollo_contact_resolver,
    form_d_sweep,
    fund_verifier,
    people_move_detector,
    spinout_watcher,
)
from gtm.skills._shared.context import RepoBundle, SkillResult, open_run
from gtm.skills._shared.sources import SourceBundle

REPO_ROOT = Path(__file__).resolve().parents[2]


def _stage(name, fn, sources, db, dry_run, summary, **kwargs) -> SkillResult | None:
    try:
        with open_run(name, sources=sources, db=db, dry_run=dry_run) as ctx:
            result = fn(ctx, **kwargs)
        summary[name] = {
            "status": result.status,
            "processed": result.records_processed,
            "signals": len(result.signals_emitted),
            "errors": len(result.errors),
        }
        return result
    except Exception as exc:
        summary[name] = {"status": "failed", "error": str(exc)}
        notify(sources, f":rotating_light: morning sweep stage {name} FAILED: {exc}")
        return None


def run_sweep(
    sources: SourceBundle,
    db: RepoBundle | None = None,
    dry_run: bool = False,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    db = db or RepoBundle()
    summary: dict[str, Any] = {}

    # ── discovery ────────────────────────────────────────────────────────────
    # LOOKBACK_DAYS env widens the Form D window (cloud backfill / catch-up
    # after a gap); defaults to the skill config (1 day).
    import os
    lookback_env = os.environ.get("LOOKBACK_DAYS")
    sweep_kwargs = {"lookback_days": int(lookback_env)} if lookback_env else {}
    sweep = _stage("form_d_sweep", form_d_sweep.run, sources, db, dry_run, summary, **sweep_kwargs)
    _stage("spinout_watcher", spinout_watcher.run, sources, db, dry_run, summary)
    _stage("people_move_detector", people_move_detector.run, sources, db, dry_run, summary)

    # ── score the funds discovery touched ────────────────────────────────────
    new_fund_ids = (sweep.metadata.get("fund_ids", []) if sweep else [])
    scored: list[dict[str, Any]] = []
    for fund_id in new_fund_ids:
        result = _stage(
            "adv_fit_scorer", adv_fit_scorer.run, sources, db, dry_run,
            summary.setdefault("adv_fit_scorer_runs", {}), fund_id=fund_id,
        )
        # New launches usually have no ADV yet (registration lags the Form D
        # by months) — absence of a score must NOT drop them from the
        # verification/resolution pipeline. fit=None means "too new to score".
        if result:
            scored.append({"fund_id": fund_id,
                           "fit": result.metadata.get("fit_score"),
                           "tier": result.metadata.get("tier") or 4})
    summary["scored"] = scored

    # ── verify: is it actually a hedge fund? ─────────────────────────────────
    # Form D self-classification lies. Rejected funds (real estate / private
    # credit / PE / VC) get no further effort, ever — the verdict compounds in
    # funds.metadata.verification + config/verifications.json.
    verifications: dict[str, dict[str, Any]] = {}
    for entry in scored:
        result = _stage(
            "fund_verifier", fund_verifier.run, sources, db, dry_run,
            summary.setdefault("fund_verifier_runs", {}), fund_id=entry["fund_id"],
        )
        if result:
            verifications[entry["fund_id"]] = {
                "is_hedge_fund": result.metadata.get("is_hedge_fund"),
                "business": result.metadata.get("business", ""),
            }
    rejected = {fid: v for fid, v in verifications.items() if v["is_hedge_fund"] is False}
    summary["verifications"] = verifications
    summary["rejected"] = rejected

    # ── resolve contacts on EVERY VERIFIED new launch ────────────────────────
    # The mission is connecting with launches; the play differs by strategy
    # (Clarion PMS for multi-asset/macro/credit, network/cap-intro for L/S
    # equity — see voice.md Mission) but tier informs urgency, not inclusion.
    resolved: list[dict[str, Any]] = []
    for entry in scored:
        if entry["fund_id"] in rejected:
            continue  # verified non-target: zero further effort
        verdict = verifications.get(entry["fund_id"], {})
        if verdict.get("is_hedge_fund") is not True:
            continue  # unverified -> orchestrated session verifies before any spend
        fund = db.funds.get(UUID(entry["fund_id"]))
        if fund is None:
            continue
        result = _stage(
            "apollo_contact_resolver", apollo_contact_resolver.run, sources, db,
            dry_run, summary.setdefault("apollo_contact_resolver_runs", {}),
            fund_id=entry["fund_id"],
        )
        if result:
            resolved.append({"fund_id": entry["fund_id"], "tier": entry["tier"],
                             **result.metadata})
    summary["resolved"] = resolved

    # ── drafting queue (orchestrated session picks these up) ─────────────────
    from gtm.skills._shared.context import load_config
    from gtm.skills.outreach_drafter import select_angle

    drafter_cfg = load_config("outreach_drafter")
    queue: list[dict[str, Any]] = []
    if not dry_run:
        urgent = db.signals.list_active(limit=50)
        for signal in urgent:
            if signal.fund_id is None:
                continue
            signal_fund = db.funds.get(signal.fund_id)
            verification = (signal_fund.metadata.get("verification") if signal_fund else None) or {}
            if verification.get("is_hedge_fund") is False:
                continue  # verified non-target never reaches the queue
            needs_verification = verification.get("is_hedge_fund") is not True
            # strategy hints from verification sharpen angle selection
            strategies = list(signal_fund.strategies) if signal_fund else []
            strategies += verification.get("strategy_hints", [])
            angle = select_angle(strategies, drafter_cfg)["key"]
            people = (
                db.client.table("people").select("id, full_name, linkedin_url")
                .eq("current_fund_id", str(signal.fund_id))
                .eq("is_buying_committee_member", True)
                .not_.is_("linkedin_url", "null")
                .is_("deleted_at", "null").limit(5).execute()
            ).data
            for person in people:
                existing = (
                    db.client.table("drafts").select("id", count="exact")
                    .eq("person_id", person["id"]).eq("signal_id", str(signal.id))
                    .is_("deleted_at", "null").execute()
                )
                if (existing.count or 0) == 0:
                    queue.append({
                        "person_id": person["id"], "person": person["full_name"],
                        "signal_id": str(signal.id), "signal_type": signal.signal_type,
                        "urgency": signal.urgency.value, "angle": angle,
                        "needs_verification": needs_verification,
                    })
    summary["draft_queue"] = queue

    # ── digest ───────────────────────────────────────────────────────────────
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [f"# Morning sweep digest — {today}", ""]
    for stage_name in ("form_d_sweep", "spinout_watcher", "people_move_detector"):
        stage = summary.get(stage_name, {})
        lines.append(
            f"- **{stage_name}**: {stage.get('status', 'skipped')} · "
            f"{stage.get('processed', 0)} processed · {stage.get('signals', 0)} signals"
            + (f" · {stage['error']}" if "error" in stage else "")
        )
    lines += ["", f"## Scored funds ({len(scored)})"]
    for entry in scored:
        fund = db.funds.get(UUID(entry["fund_id"]))
        lines.append(f"- {fund.legal_name if fund else entry['fund_id']}: "
                     f"fit {entry['fit']} -> tier {entry['tier']}")
    if rejected:
        lines += ["", f"## Rejected by verification ({len(rejected)}) — no effort spent"]
        for fund_id, verdict in rejected.items():
            fund = db.funds.get(UUID(fund_id))
            lines.append(f"- {fund.legal_name if fund else fund_id}: {verdict['business']}")

    lines += ["", f"## Drafting queue ({len(queue)}) — run an orchestrated session to draft"]
    for item in queue[:20]:
        flag = " · VERIFY FIRST" if item.get("needs_verification") else ""
        lines.append(f"- {item['person']} · {item['signal_type']} ({item['urgency']}) · "
                     f"angle={item['angle']}{flag} · "
                     f"person_id={item['person_id']} signal_id={item['signal_id']}")
    if dry_run:
        lines += ["", "_DRY RUN — no entity writes performed._"]

    digest = "\n".join(lines) + "\n"
    out_dir = out_dir or REPO_ROOT / "gtm" / "briefs"
    out_dir.mkdir(parents=True, exist_ok=True)
    digest_path = out_dir / f"digest_{today}.md"
    digest_path.write_text(digest, encoding="utf-8")
    summary["digest_path"] = str(digest_path)

    notify(
        sources,
        f"Morning sweep {today}: "
        f"{summary.get('form_d_sweep', {}).get('signals', 0)} Form D signals, "
        f"{len(scored)} funds scored, {len(queue)} drafts queued — {digest_path.name}",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sources, missing = build_sources()
    if missing:
        print("sources unavailable (skills degrade):", "; ".join(missing))
    summary = run_sweep(sources, dry_run=args.dry_run)
    print(f"digest: {summary['digest_path']}")
    failed = [k for k, v in summary.items() if isinstance(v, dict) and v.get("status") == "failed"]
    print("stages failed:", failed or "none")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
