"""Command-line entrypoints for the Coremont Signal Engine.

    python -m app.cli initdb              # create tables
    python -m app.cli ingest --seed       # run Jobs 1-5 on bundled sample data
    python -m app.cli ingest              # run Jobs 1-5 against live SEC EDGAR
    python -m app.cli ingest --lookback 3 # live, last 3 days
    python -m app.cli export --min-tier 2 # re-run Job 5 (CSV) only
    python -m app.cli digest              # refresh data + build/email daily digest
    python -m app.cli digest --seed       # same, using bundled sample data
    python -m app.cli digest --no-refresh # rebuild digest from current DB only
    python -m app.cli stats               # quick DB summary
"""
from __future__ import annotations

import argparse
import json
import sys

from .db import init_db, session_scope
from .ingestion import export_job, pipeline


def _cmd_initdb(_args) -> int:
    init_db()
    print("Database initialized.")
    return 0


def _cmd_ingest(args) -> int:
    init_db()
    result = pipeline.run_pipeline(
        seed=args.seed, lookback_days=args.lookback, export_min_tier=args.min_tier
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


def _cmd_export(args) -> int:
    init_db()
    with session_scope() as session:
        path = export_job.write_csv(session, min_tier=args.min_tier)
        rows = export_job.build_rows(session, min_tier=args.min_tier)
    print(f"Wrote {len(rows)} rows to {path}")
    return 0


def _cmd_digest(args) -> int:
    from . import digest as digest_mod
    from . import notify
    from .db import session_scope

    init_db()

    # 1. Refresh data unless asked to skip.
    if not args.no_refresh:
        if args.seed:
            pipeline.run_pipeline(seed=True, export_min_tier=args.min_tier)
        else:
            try:
                pipeline.run_pipeline(seed=False, lookback_days=args.lookback,
                                      export_min_tier=args.min_tier)
            except Exception as exc:  # noqa: BLE001 — degrade to seed if SEC unreachable
                print(f"Live ingest failed ({exc}); falling back to bundled seed.")
                pipeline.run_pipeline(seed=True, export_min_tier=args.min_tier)

    # 2. Build + render the digest.
    with session_scope() as session:
        d = digest_mod.build_digest(session, min_tier=args.min_tier)
        html_path = digest_mod.write_html_file(d)
        subject = digest_mod.subject(d)
        html_body = digest_mod.render_html(d)
        text_body = digest_mod.render_text(d)

    # 3. Email (unless suppressed), then advance the "new since" baseline.
    email_status = {"sent": False, "reason": "suppressed (--no-email)"}
    if not args.no_email:
        email_status = notify.send_digest_email(subject, html_body, text_body)

    with session_scope() as session:
        digest_mod.save_state(session, min_tier=args.min_tier)

    print(json.dumps(
        {
            "subject": subject,
            "tier1": d.tier1,
            "tier2": d.tier2,
            "new": d.new_count,
            "first_run": d.first_run,
            "html_file": html_path,
            "email": email_status,
        },
        indent=2,
        default=str,
    ))
    return 0


def _cmd_stats(_args) -> int:
    from sqlalchemy import func, select

    from .models import Filing, FundVehicle, Manager, Signal

    init_db()
    with session_scope() as session:
        managers = session.scalar(select(func.count()).select_from(Manager))
        vehicles = session.scalar(select(func.count()).select_from(FundVehicle))
        filings = session.scalar(select(func.count()).select_from(Filing))
        sigs = session.scalar(select(func.count()).select_from(Signal))
        tiers = {}
        for t in (1, 2, 3, 4):
            tiers[t] = session.scalar(
                select(func.count()).select_from(Manager).where(Manager.tier == t)
            )
    print(
        json.dumps(
            {
                "managers": managers,
                "vehicles": vehicles,
                "filings": filings,
                "signals": sigs,
                "tiers": tiers,
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coremont", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("initdb", help="create database tables")

    p_ingest = sub.add_parser("ingest", help="run the daily pipeline (Jobs 1-5)")
    p_ingest.add_argument("--seed", action="store_true", help="use bundled sample data")
    p_ingest.add_argument("--lookback", type=int, default=None, help="days back to scan (live)")
    p_ingest.add_argument("--min-tier", type=int, default=2, help="export tier threshold")

    p_export = sub.add_parser("export", help="write the CSV export queue (Job 5)")
    p_export.add_argument("--min-tier", type=int, default=2)

    p_digest = sub.add_parser("digest", help="refresh data and build/email the daily digest")
    p_digest.add_argument("--seed", action="store_true", help="refresh from bundled sample data")
    p_digest.add_argument("--no-refresh", action="store_true", help="skip ingestion; rebuild from current DB")
    p_digest.add_argument("--no-email", action="store_true", help="write the HTML file but don't send email")
    p_digest.add_argument("--lookback", type=int, default=None, help="days back to scan (live)")
    p_digest.add_argument("--min-tier", type=int, default=2, help="include managers up to this tier")

    sub.add_parser("stats", help="print a DB summary")

    args = parser.parse_args(argv)
    handlers = {
        "initdb": _cmd_initdb,
        "ingest": _cmd_ingest,
        "export": _cmd_export,
        "digest": _cmd_digest,
        "stats": _cmd_stats,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
