"""Web search via Tavily (chosen over Serper: agent-oriented, returns cleaned
page content rather than bare SERP links — spinout/displacement parsing needs
the content, and the free tier covers initial volume).

Same guarantees as every source: rate-limited, tenacity-retried, raw response
archived to raw_payloads before parsing, normalized SearchResult output.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from uuid import UUID

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gtm.db.repositories.runs import RunsRepo

TAVILY_URL = "https://api.tavily.com/search"

_retry = retry(
    retry=retry_if_exception_type((httpx.HTTPError, ConnectionError, TimeoutError)),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)


class SearchResult(BaseModel):
    title: str
    url: str
    content: str
    score: float = 0.0
    published_date: str | None = None


class TavilySearch:
    SOURCE = "web_search"

    def __init__(
        self,
        runs_repo: RunsRepo | None = None,
        api_key: str | None = None,
        requests_per_minute: float = 60,
    ) -> None:
        load_dotenv(encoding="utf-8-sig")
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("TAVILY_API_KEY missing from environment/.env")
        self.runs = runs_repo or RunsRepo()
        self.current_run_id: UUID | None = None
        self._min_interval = 60.0 / requests_per_minute
        self._last = 0.0
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=45)

    def _wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.monotonic()

    @_retry
    def search(
        self,
        query: str,
        max_results: int = 8,
        days: int | None = None,
        topic: str = "general",
        include_domains: list[str] | None = None,
    ) -> list[SearchResult]:
        self._wait()
        body: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "topic": topic,
            "search_depth": "basic",
        }
        if days is not None:
            body["topic"] = "news"
            body["days"] = days
        if include_domains:
            body["include_domains"] = include_domains
        resp = self._http.post(
            TAVILY_URL,
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            self.runs.archive_payload(
                source=self.SOURCE,
                response=data,
                request={"op": "search", **body},
                source_run_id=self.current_run_id,
            )
        except Exception:
            pass
        return [
            SearchResult(
                title=item.get("title") or "",
                url=item.get("url") or "",
                content=item.get("content") or "",
                score=float(item.get("score") or 0),
                published_date=item.get("published_date"),
            )
            for item in data.get("results", [])
            if item.get("url")
        ]
