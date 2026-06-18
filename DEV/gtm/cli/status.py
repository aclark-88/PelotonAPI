"""Last N source_runs at a glance.

    py -m gtm.cli.status [--limit 20] [--skill form_d_sweep]
"""

from __future__ import annotations

import argparse
from datetime import datetime

from gtm.db.client import get_client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--skill", default=None)
    args = parser.parse_args()

    query = (
        get_client().table("source_runs").select("*")
        .order("started_at", desc=True).limit(args.limit)
    )
    if args.skill:
        query = query.eq("skill_name", args.skill)
    runs = query.execute().data

    header = f"{'started (UTC)':20} {'skill':28} {'status':8} {'dur':>6} {'proc':>5} {'ins':>4} {'upd':>4} {'errs':>4}  run_id"
    print(header)
    print("-" * len(header))
    for run in runs:
        started = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
        duration = "-"
        if run.get("ended_at"):
            ended = datetime.fromisoformat(run["ended_at"].replace("Z", "+00:00"))
            duration = f"{(ended - started).total_seconds():.0f}s"
        print(
            f"{started.strftime('%Y-%m-%d %H:%M:%S'):20} {run['skill_name'][:28]:28} "
            f"{run['status']:8} {duration:>6} {run['records_processed']:>5} "
            f"{run['records_inserted']:>4} {run['records_updated']:>4} "
            f"{len(run.get('errors') or []):>4}  {run['id']}"
        )
        for error in (run.get("errors") or [])[:2]:
            print(f"{'':20}   !! {str(error)[:100]}")


if __name__ == "__main__":
    main()
