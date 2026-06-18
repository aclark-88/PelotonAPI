"""Command-line entrypoints for the Coremont Signal Engine.

    python -m app.cli initdb              # create tables
    python -m app.cli ingest --seed       # run Jobs 1-5 on bundled sample data
    python -m app.cli ingest              # run Jobs 1-5 against live SEC EDGAR
    python -m app.cli ingest --lookback 3 # live, last 3 days
    python -m app.cli export --min-tier 2 # re-run Job 5 (CSV) only
    python -m app.cli digest              # refresh data + build/email daily digest
    python -m app.cli digest --seed       # same, using bundled sample data
    python -m app.cli digest --no-refresh # rebuild digest from current DB only
    python -m app.cli verify              # fetch REAL Form D filings from SEC + source URLs
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
        seed=args.seed,
        search=args.search,
        days=args.days,
        lookback_days=args.lookback,
        export_min_tier=args.min_tier,
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
                # Targeted ICP search over the last N days = relevant managers.
                pipeline.run_pipeline(search=True, days=args.days,
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


def _cmd_verify(args) -> int:
    """Fetch real Form D filings from SEC and print them with source URLs so the
    parsed values can be cross-checked against the live SEC website. Requires
    outbound access to www.sec.gov (works on a normal network; blocked in some
    sandboxes)."""
    import datetime as dt

    from .ingestion.edgar_client import EdgarClient

    print(f"Querying SEC EDGAR daily index for the last {args.lookback} business day(s)…\n")
    found = 0
    with EdgarClient() as client:
        today = dt.date.today()
        for delta in range(args.lookback):
            day = today - dt.timedelta(days=delta)
            if day.weekday() >= 5:
                continue
            try:
                entries = client.fetch_daily_index(day)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{day}] index unavailable: {exc}")
                continue
            print(f"  [{day}] {len(entries)} Form D / D/A filings in daily index")
            for e in entries[: args.limit - found if args.limit else None]:
                try:
                    rec = client.fetch_form_d(e.cik, e.accession_no, e.date_filed)
                except Exception as exc:  # noqa: BLE001
                    print(f"    ! could not fetch {e.accession_no}: {exc}")
                    continue
                acc_nodash = rec.accession_no.replace("-", "")
                url = (
                    f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                    f"&CIK={rec.cik}&type=D&dateb=&owner=include&count=10"
                )
                doc = (
                    f"https://www.sec.gov/Archives/edgar/data/{rec.cik}/{acc_nodash}/"
                    "primary_doc.xml"
                )
                print(f"\n  ── {rec.issuer_name}")
                print(f"     CIK {rec.cik} · accession {rec.accession_no} · "
                      f"{'AMENDMENT' if rec.is_amendment else 'new'}")
                print(f"     filed {rec.filing_date} · first sale {rec.first_sale_date}")
                print(f"     offering {rec.offering_amount} · sold {rec.amount_sold}")
                print(f"     verify ► {url}")
                print(f"     source ► {doc}")
                found += 1
                if args.limit and found >= args.limit:
                    break
            if args.limit and found >= args.limit:
                break

    if found == 0:
        print("\nNo filings fetched. If every line said 'index unavailable', this network "
              "cannot reach www.sec.gov (e.g. a sandbox allowlist). Run this on your own "
              "machine, where the SEC API is publicly reachable.")
    else:
        print(f"\nFetched {found} real Form D filing(s). Open the 'verify' URLs to confirm "
              "the parsed values match SEC EDGAR exactly.")
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
    p_ingest.add_argument("--search", action="store_true",
                          help="targeted: pull only Form D filings matching ICP terms (recommended)")
    p_ingest.add_argument("--days", type=int, default=90, help="search look-back window in days (--search)")
    p_ingest.add_argument("--lookback", type=int, default=None, help="business days back to scan (full firehose)")
    p_ingest.add_argument("--min-tier", type=int, default=2, help="export tier threshold")

    p_export = sub.add_parser("export", help="write the CSV export queue (Job 5)")
    p_export.add_argument("--min-tier", type=int, default=2)

    p_digest = sub.add_parser("digest", help="refresh data and build/email the daily digest")
    p_digest.add_argument("--seed", action="store_true", help="refresh from bundled sample data")
    p_digest.add_argument("--no-refresh", action="store_true", help="skip ingestion; rebuild from current DB")
    p_digest.add_argument("--no-email", action="store_true", help="write the HTML file but don't send email")
    p_digest.add_argument("--days", type=int, default=90, help="ICP search look-back window (days)")
    p_digest.add_argument("--min-tier", type=int, default=2, help="include managers up to this tier")

    p_verify = sub.add_parser("verify", help="fetch real Form D filings from SEC and print with source URLs")
    p_verify.add_argument("--lookback", type=int, default=4, help="business days back to scan")
    p_verify.add_argument("--limit", type=int, default=5, help="max filings to fetch")

    sub.add_parser("stats", help="print a DB summary")

    args = parser.parse_args(argv)
    handlers = {
        "initdb": _cmd_initdb,
        "ingest": _cmd_ingest,
        "export": _cmd_export,
        "digest": _cmd_digest,
        "verify": _cmd_verify,
        "stats": _cmd_stats,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
