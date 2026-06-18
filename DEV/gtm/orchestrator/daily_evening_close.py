"""Daily 6pm ET close: light hygiene pass + queue tomorrow's meeting briefs.

    py -m gtm.orchestrator.daily_evening_close [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from gtm.orchestrator._sources import build_sources, notify
from gtm.skills import meeting_brief_generator, pipeline_hygiene_auditor
from gtm.skills._shared.context import RepoBundle, open_run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sources, missing = build_sources()
    if missing:
        print("sources unavailable:", "; ".join(missing))
    db = RepoBundle()

    # light hygiene pass (no Slack spam in the evening — Monday gets the loud one)
    with open_run(
        "pipeline_hygiene_auditor", sources=sources, db=db, dry_run=args.dry_run,
        config_overrides={"post_to_slack": False},
    ) as ctx:
        hygiene = pipeline_hygiene_auditor.run(ctx)
    print(f"hygiene: {hygiene.status}, flagged {len(hygiene.metadata.get('flagged', []))}")

    # queue tomorrow's meeting briefs from HubSpot calendar
    briefs = 0
    if sources.hubspot is not None:
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
        try:
            # meetings search: start time within tomorrow (UTC bounds)
            start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)
            body = {
                "filterGroups": [{"filters": [
                    {"propertyName": "hs_meeting_start_time", "operator": "BETWEEN",
                     "value": str(int(start.timestamp() * 1000)),
                     "highValue": str(int((start + timedelta(days=1)).timestamp() * 1000))},
                ]}],
                "properties": ["hs_meeting_title", "hs_meeting_start_time"],
                "limit": 20,
            }
            resp = sources.hubspot._http.post(
                "https://api.hubapi.com/crm/v3/objects/meetings/search", json=body
            )
            resp.raise_for_status()
            meetings = resp.json().get("results", [])
            for meeting in meetings:
                with open_run(
                    "meeting_brief_generator", sources=sources, db=db, dry_run=args.dry_run
                ) as ctx:
                    result = meeting_brief_generator.run(ctx, meeting_id=meeting["id"])
                if result.status == "success" and result.metadata.get("attendee_count"):
                    briefs += 1
        except Exception as exc:
            print(f"meeting queue failed: {exc}")
    print(f"briefs generated for tomorrow: {briefs}")
    notify(sources, f"Evening close: {len(hygiene.metadata.get('flagged', []))} deals flagged, "
                    f"{briefs} briefs prepared for tomorrow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
