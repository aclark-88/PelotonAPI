"""Monday 7am ET: full pipeline hygiene + opp-at-risk digest to Slack.

    py -m gtm.orchestrator.weekly_monday_brief [--dry-run]
"""

from __future__ import annotations

import argparse
import sys

from gtm.orchestrator._sources import build_sources, notify
from gtm.skills import pipeline_hygiene_auditor
from gtm.skills._shared.context import RepoBundle, open_run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sources, missing = build_sources()
    if missing:
        print("sources unavailable:", "; ".join(missing))
    db = RepoBundle()

    with open_run(
        "pipeline_hygiene_auditor", sources=sources, db=db, dry_run=args.dry_run
    ) as ctx:
        result = pipeline_hygiene_auditor.run(ctx)

    flagged = result.metadata.get("flagged", [])
    print(f"hygiene: {result.status}, {len(flagged)} flagged, report {result.metadata.get('report_path')}")
    if flagged:
        detail = "\n".join(f"• {f['name']}: {', '.join(f['flags'])}" for f in flagged[:15])
        notify(sources, f"Monday pipeline review — {len(flagged)} deals at risk:\n{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
