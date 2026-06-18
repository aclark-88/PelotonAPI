"""Webhook receivers: HeyReach replies, HubSpot meeting bookings.

Handler functions are transport-agnostic (testable directly); the __main__
block serves them on a minimal stdlib HTTP server for local/ngrok use:

    py -m gtm.orchestrator.event_handlers --port 8787

Routes:
  POST /webhooks/heyreach   -> handle_heyreach_reply
  POST /webhooks/hubspot    -> handle_hubspot_meeting

(No Apollo handler: the campaign is LinkedIn-only; Apollo is enrichment, not
a sending channel.) Production hosting (Supabase Edge Function or a small
VPS) is a deploy decision documented in docs/scheduling.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from gtm.models.common import OutreachStatus, ReplyIntent, ReplySentiment
from gtm.models.outreach import ReplyIn
from gtm.orchestrator._sources import build_sources, notify
from gtm.skills import meeting_brief_generator
from gtm.skills._shared.context import RepoBundle, open_run


def handle_heyreach_reply(payload: dict[str, Any], db: RepoBundle | None = None) -> dict[str, Any]:
    """HeyReach reply webhook -> replies row + attempt status + Slack ping.

    Sentiment/intent stay null here — classification is an orchestrated-session
    task (the reply lands in the queue via the attempt's 'replied' status)."""
    db = db or RepoBundle()
    body = payload.get("message") or payload.get("messageText") or ""
    # Canonical-campaign pattern: every attempt shares the campaign id, so the
    # lead's profile URL is the identity key.
    lead = payload.get("lead") or {}
    profile_url = (
        lead.get("profileUrl") or payload.get("leadProfileUrl")
        or payload.get("profileUrl") or ""
    ).strip()
    attempt = None
    if profile_url:
        people_rows = (
            db.client.table("people").select("id")
            .eq("linkedin_url", profile_url).is_("deleted_at", "null").limit(1).execute()
        ).data
        if people_rows:
            attempt_rows = (
                db.client.table("outreach_attempts").select("*")
                .eq("person_id", people_rows[0]["id"]).eq("channel", "linkedin")
                .is_("deleted_at", "null").order("created_at", desc=True).limit(1).execute()
            ).data
            attempt = attempt_rows[0] if attempt_rows else None
    if attempt is None:
        # fallback: legacy per-prospect campaigns keyed by external_id
        campaign_id = str(payload.get("campaignId") or payload.get("campaign_id") or "")
        if campaign_id:
            attempt_rows = (
                db.client.table("outreach_attempts").select("*")
                .eq("external_id", campaign_id).is_("deleted_at", "null").limit(1).execute()
            ).data
            attempt = attempt_rows[0] if attempt_rows else None
    if attempt is None:
        return {"ok": False, "error": f"no attempt matched (profile={profile_url or '?'})"}

    reply = db.outreach.record_reply(
        ReplyIn(
            outreach_attempt_id=UUID(attempt["id"]),
            body=body,
            received_at=datetime.now(timezone.utc),
            metadata={"raw_webhook": {k: v for k, v in payload.items() if k != "message"}},
        )
    )
    return {"ok": True, "reply_id": str(reply.id), "attempt_id": attempt["id"]}


def handle_hubspot_meeting(payload: dict[str, Any], db: RepoBundle | None = None) -> dict[str, Any]:
    """HubSpot meeting-created webhook -> immediate meeting brief."""
    db = db or RepoBundle()
    meeting_id = str(payload.get("objectId") or payload.get("meeting_id") or "")
    if not meeting_id:
        return {"ok": False, "error": "no objectId in payload"}
    sources, _ = build_sources()
    with open_run("meeting_brief_generator", sources=sources, db=db) as ctx:
        result = meeting_brief_generator.run(ctx, meeting_id=meeting_id)
    return {"ok": result.status == "success", "brief": result.metadata.get("brief_path"),
            "errors": result.errors}


def main() -> None:
    import argparse
    from http.server import BaseHTTPRequestHandler, HTTPServer

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    db = RepoBundle()
    sources, _ = build_sources()

    routes = {
        "/webhooks/heyreach": handle_heyreach_reply,
        "/webhooks/hubspot": handle_hubspot_meeting,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            handler = routes.get(self.path)
            if handler is None:
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = handler(payload, db=db)
                if self.path.endswith("heyreach") and result.get("ok"):
                    notify(sources, f":speech_balloon: LinkedIn reply received "
                                    f"(attempt {result.get('attempt_id')}) — triage in next session")
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            self.send_response(200 if result.get("ok") else 422)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        def log_message(self, *_args) -> None:
            pass

    print(f"webhook server on :{args.port} — routes: {list(routes)}")
    HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
