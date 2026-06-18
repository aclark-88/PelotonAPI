"""Slack webhook notifier (config-driven, optional).

Constructed only when SLACK_WEBHOOK_URL is set; skills require('slack') and
degrade gracefully when it's absent. Used for: skill failures, immediate
signals, daily digests, weekly briefs.
"""

from __future__ import annotations

import os
from uuid import UUID

import httpx
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gtm.db.repositories.runs import RunsRepo

_retry = retry(
    retry=retry_if_exception_type((httpx.HTTPError, ConnectionError, TimeoutError)),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)


class SlackNotifier:
    SOURCE = "slack"

    def __init__(self, runs_repo: RunsRepo | None = None, webhook_url: str | None = None) -> None:
        load_dotenv(encoding="utf-8-sig")
        self.webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
        if not self.webhook_url:
            raise RuntimeError("SLACK_WEBHOOK_URL missing from environment/.env")
        self.runs = runs_repo or RunsRepo()
        self.current_run_id: UUID | None = None
        self._http = httpx.Client(timeout=20)

    @_retry
    def post(self, text: str) -> bool:
        resp = self._http.post(self.webhook_url, json={"text": text})
        resp.raise_for_status()
        try:
            self.runs.archive_payload(
                source=self.SOURCE,
                response={"status": resp.status_code},
                request={"op": "post", "chars": len(text)},  # text itself lives in briefs/
                source_run_id=self.current_run_id,
            )
        except Exception:
            pass
        return True
