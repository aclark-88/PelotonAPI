"""Apollo REST client (X-Api-Key auth; key validated against auth/health).

Typed methods over raw dicts; every response archived to raw_payloads.
Credit awareness: people/match (enrich) consumes credits; mixed_people search
is cheaper. Skills cap volumes via their configs, not here.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from uuid import UUID

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gtm.db.repositories.runs import RunsRepo

BASE = "https://api.apollo.io/api/v1"

_retry = retry(
    retry=retry_if_exception_type((httpx.HTTPError, ConnectionError, TimeoutError)),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(4),
    reraise=True,
)


class ApolloPerson(BaseModel):
    apollo_id: str
    name: str
    title: str | None = None
    linkedin_url: str | None = None
    email: str | None = None
    organization_name: str | None = None
    organization_domain: str | None = None
    seniority: str | None = None
    employment_history: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


def _to_person(p: dict[str, Any]) -> ApolloPerson:
    org = p.get("organization") or {}
    return ApolloPerson(
        apollo_id=str(p.get("id") or ""),
        name=p.get("name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
        title=p.get("title"),
        linkedin_url=p.get("linkedin_url"),
        email=p.get("email") if p.get("email") not in ("email_not_unlocked@domain.com",) else None,
        organization_name=org.get("name") or p.get("organization_name"),
        organization_domain=org.get("primary_domain"),
        seniority=p.get("seniority"),
        employment_history=p.get("employment_history") or [],
        raw={k: v for k, v in p.items() if k not in ("organization",)},
    )


class ApolloClient:
    SOURCE = "apollo"

    def __init__(
        self,
        runs_repo: RunsRepo | None = None,
        api_key: str | None = None,
        requests_per_minute: float = 50,
    ) -> None:
        load_dotenv(encoding="utf-8-sig")
        self.api_key = api_key or os.environ.get("APOLLO_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("APOLLO_API_KEY missing from environment/.env")
        self.runs = runs_repo or RunsRepo()
        self.current_run_id: UUID | None = None
        self._min_interval = 60.0 / requests_per_minute
        self._last = 0.0
        self._lock = threading.Lock()
        self._http = httpx.Client(
            timeout=45,
            headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"},
        )

    def _wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.monotonic()

    def _post(self, path: str, body: dict[str, Any], op: str) -> dict[str, Any]:
        self._wait()
        resp = self._http.post(f"{BASE}{path}", json=body)
        resp.raise_for_status()
        data = resp.json()
        try:
            self.runs.archive_payload(
                source=self.SOURCE,
                response=data,
                request={"op": op, "path": path, **{k: v for k, v in body.items() if k != "api_key"}},
                source_run_id=self.current_run_id,
            )
        except Exception:
            pass
        return data

    @_retry
    def search_people(
        self,
        domain: str | None = None,
        org_name: str | None = None,
        titles: list[str] | None = None,
        per_page: int = 10,
    ) -> list[ApolloPerson]:
        """Current people at an org matching title keywords (mixed_people search)."""
        body: dict[str, Any] = {"page": 1, "per_page": per_page}
        if domain:
            body["q_organization_domains_list"] = [domain]
        elif org_name:
            body["q_organization_name"] = org_name
        if titles:
            body["person_titles"] = titles
        # NB: /mixed_people/search is deprecated for API callers (422)
        data = self._post("/mixed_people/api_search", body, op="search_people")
        return [_to_person(p) for p in data.get("people", [])]

    @_retry
    def enrich_person(
        self,
        name: str | None = None,
        domain: str | None = None,
        linkedin_url: str | None = None,
        email: str | None = None,
        person_id: str | None = None,
    ) -> ApolloPerson | None:
        """people/match — consumes an enrichment credit on success.
        Spend is pre-approved (standing permission, Alex 2026-06-11)."""
        body: dict[str, Any] = {}
        if person_id:
            body["id"] = person_id
        if name:
            body["name"] = name
        if domain:
            body["domain"] = domain
        if linkedin_url:
            body["linkedin_url"] = linkedin_url
        if email:
            body["email"] = email
        if not body:
            return None
        data = self._post("/people/match", body, op="enrich_person")
        person = data.get("person")
        return _to_person(person) if person else None
