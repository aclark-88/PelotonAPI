"""Layer 3 tool: read/write the manager-verification store.

The morning brief treats ``config/verifications.json`` as authoritative — a
verdict here overrides the Form D name/type heuristics. This tool lets an agent
(or you) record verdicts cleanly and list the candidates still needing review,
so verification knowledge accumulates across days instead of being re-derived.

CLI:
    python tools/verify_store.py pending          # candidates lacking a verdict
    python tools/verify_store.py list             # all recorded verdicts
    python tools/verify_store.py set --cik 2064620 --target false \
        --business "Real-estate / HTC private lender (Octagon Finance)"

Returns the shared JSON envelope.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from typing import Any

from _shared import BRIEFS_DIR, CONFIG_DIR, ensure_dirs, fatal, ok, run_cli, skip

STORE = CONFIG_DIR / "verifications.json"
LATEST = BRIEFS_DIR / "latest.json"

_DEFAULT_COMMENT = (
    "Verification overrides keyed by issuer CIK (leading zeros stripped). "
    "is_target=false DROPS the candidate (authoritative, beats name/type "
    "heuristics); is_target=true KEEPS and promotes it; a CIK absent here is "
    "treated as 'unverified - needs review'. Written by web/agent verification."
)


def _norm_cik(cik: str) -> str:
    s = str(cik).strip()
    return str(int(s)) if s.isdigit() else s


def _load() -> dict[str, Any]:
    if not STORE.exists():
        return {"_comment": _DEFAULT_COMMENT}
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"cannot read {STORE}: {exc}") from exc


def _save(data: dict[str, Any]) -> None:
    ensure_dirs()
    STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def set_verdict(cik: str, is_target: bool, business: str, by: str = "agent") -> dict[str, Any]:
    if not cik:
        return skip("a CIK is required")
    if not business:
        return skip("a 'business' note is required (what the manager actually is)")
    try:
        data = _load()
    except RuntimeError as exc:
        return fatal(str(exc))
    key = _norm_cik(cik)
    data[key] = {
        "is_target": bool(is_target),
        "business": business,
        "verified_by": by,
        "date": dt.date.today().isoformat(),
    }
    _save(data)
    return ok({"cik": key, **data[key]})


def list_verdicts() -> dict[str, Any]:
    try:
        data = _load()
    except RuntimeError as exc:
        return fatal(str(exc))
    items = {k: v for k, v in data.items() if not k.startswith("_")}
    return ok({"count": len(items), "verdicts": items})


def list_pending() -> dict[str, Any]:
    """Greenfield candidates in the latest brief that have no verdict yet."""
    if not LATEST.exists():
        return skip("no briefs/latest.json yet; run tools/morning_brief.py first")
    try:
        data = _load()
        brief = json.loads(LATEST.read_text(encoding="utf-8"))
    except (RuntimeError, json.JSONDecodeError, OSError) as exc:
        return fatal(f"cannot read store/brief: {exc}")
    known = {k for k in data if not k.startswith("_")}
    # The brief holds unverified candidates in a dedicated 'pending' queue. Fall
    # back to scanning signals for older briefs that predate the queue.
    queue = brief.get("pending")
    if queue is None:
        queue = [
            s
            for s in brief.get("signals", [])
            if s.get("signal") == "greenfield_launch" and not s.get("verified")
        ]
    pending = [
        {
            "cik": s.get("cik"),
            "fund": s.get("fund"),
            "fund_type": s.get("fund_type"),
            "accession": s.get("accession"),
        }
        for s in queue
        if _norm_cik(s.get("cik", "")) not in known
    ]
    return ok({"count": len(pending), "pending": pending})


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="manager-verification store")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="record a verdict")
    s.add_argument("--cik", required=True)
    s.add_argument("--target", required=True, choices=["true", "false"])
    s.add_argument("--business", required=True, help="what the manager actually is")
    s.add_argument("--by", default="agent")
    sub.add_parser("list", help="list all verdicts")
    sub.add_parser("pending", help="candidates needing verification")
    return p


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = _build_parser().parse_args(argv)
    if args.cmd == "set":
        return set_verdict(args.cik, args.target == "true", args.business, args.by)
    if args.cmd == "list":
        return list_verdicts()
    if args.cmd == "pending":
        return list_pending()
    return fatal(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    run_cli(main())
