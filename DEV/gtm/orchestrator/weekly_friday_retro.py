"""Friday retro: curate reply outcomes into the voice exemplar corpus.

Pulls the week's replies, tags the drafts that earned them as wins (positive/
meeting_request) or losses (negative/unsubscribe), and files win copy into
gtm/skills/configs/voice_corpus/ — which outreach_drafter's retrieval reads
via drafts.embedding. No retraining; this just curates exemplars.

    py -m gtm.orchestrator.weekly_friday_retro [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gtm.orchestrator._sources import build_sources, notify
from gtm.skills._shared.context import CONFIGS_DIR, RepoBundle

CORPUS_DIR = CONFIGS_DIR / "voice_corpus"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    sources, _ = build_sources()
    db = RepoBundle()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()

    replies = (
        db.client.table("replies").select("*, outreach_attempts(draft_id, person_id)")
        .gte("received_at", since).is_("deleted_at", "null").execute()
    ).data
    wins, losses = [], []
    for reply in replies:
        attempt = reply.get("outreach_attempts") or {}
        draft_id = attempt.get("draft_id")
        if not draft_id:
            continue
        draft = (
            db.client.table("drafts").select("body, variant_label").eq("id", draft_id)
            .single().execute()
        ).data
        bucket = None
        if reply.get("intent") == "meeting_request" or reply.get("sentiment") == "positive":
            bucket = wins
        elif reply.get("sentiment") == "negative" or reply.get("intent") == "unsubscribe":
            bucket = losses
        if bucket is not None:
            bucket.append({"draft_id": draft_id, "body": draft["body"],
                           "variant": draft["variant_label"],
                           "sentiment": reply.get("sentiment"), "intent": reply.get("intent")})

    if not args.dry_run and wins:
        CORPUS_DIR.mkdir(parents=True, exist_ok=True)
        week = datetime.now(timezone.utc).date().isoformat()
        path = CORPUS_DIR / f"wins_{week}.md"
        lines = [f"# Winning copy — week of {week}", ""]
        for win in wins:
            lines += [f"## {win['variant']} (intent: {win['intent']}, sentiment: {win['sentiment']})",
                      "", win["body"], ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"corpus updated: {path}")

    print(f"replies: {len(replies)} | wins: {len(wins)} | losses: {len(losses)}")
    notify(sources, f"Friday retro: {len(replies)} replies this week — "
                    f"{len(wins)} wins curated, {len(losses)} losses tagged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
