"""Wednesday: competitor vendor news sweep -> signals.

Searches the week's news for each incumbent vendor in vendors.yaml (price
changes, outages, acquisitions, sunset announcements = displacement windows)
and records a manual_flag signal per finding for human triage.

    py -m gtm.orchestrator.weekly_wednesday_competitive [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import yaml

from gtm.models.common import Urgency
from gtm.models.signals import SignalIn
from gtm.orchestrator._sources import build_sources, notify
from gtm.skills._shared import dedupe
from gtm.skills._shared.context import CONFIGS_DIR, RepoBundle, open_run

QUERY = '"{vendor}" (acquisition OR outage OR "price increase" OR sunset OR migration OR layoffs)'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sources, missing = build_sources()
    if sources.web is None:
        print("web search unavailable — aborting:", "; ".join(missing))
        return 1
    db = RepoBundle()

    vendors_cfg = yaml.safe_load((CONFIGS_DIR / "vendors.yaml").read_text(encoding="utf-8"))
    vendors = [v["name"] for grp in vendors_cfg.get("vendors", {}).values() for v in grp]

    findings = 0
    with open_run("weekly_competitive_sweep", sources=sources, db=db, dry_run=args.dry_run) as ctx:
        for vendor in vendors:
            try:
                results = ctx.sources.web.search(QUERY.format(vendor=vendor), max_results=4, days=7)
            except Exception as exc:
                ctx.result.error("search", exc, vendor=vendor)
                continue
            for hit in results:
                ctx.result.records_processed += 1
                if hit.score < 0.4 or vendor.lower() not in f"{hit.title} {hit.content}".lower():
                    continue
                if args.dry_run:
                    continue
                signal = db.signals.record_signal(
                    SignalIn(
                        signal_type="manual_flag",
                        source="web_search",
                        source_record_id=dedupe.web_finding_record_id(
                            f"competitive:{vendor.lower()}", hit.url
                        ),
                        observed_at=datetime.now(timezone.utc),
                        urgency=Urgency.this_week,
                        payload={"topic": "competitor_news", "vendor": vendor,
                                 "title": hit.title, "url": hit.url,
                                 "snippet": hit.content[:400]},
                    ),
                    source_run_id=ctx.run_id,
                )
                ctx.result.emit(signal.id)
                findings += 1

    print(f"competitive findings: {findings}")
    if findings:
        notify(sources, f"Wednesday competitive sweep: {findings} vendor-news signals for triage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
