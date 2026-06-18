"""HubSpot REST client (private app token).

Read paths only, plus the (already-approved) contact write used elsewhere:
- find_contact: champion-relocation check (people_move_detector)
- open_deals / won_deals / associations / company / contacts / meeting:
  pipeline_hygiene_auditor + meeting_brief_generator
Full two-way sync is explicitly out of scope (separate worker).
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import httpx
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gtm.db.repositories.runs import RunsRepo

BASE = "https://api.hubapi.com"

_retry = retry(
    retry=retry_if_exception_type((httpx.HTTPError, ConnectionError, TimeoutError)),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)


class HubSpotClient:
    SOURCE = "hubspot"

    def __init__(self, runs_repo: RunsRepo | None = None, token: str | None = None) -> None:
        load_dotenv(encoding="utf-8-sig")
        self.token = token or os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
        if not self.token:
            raise RuntimeError("HUBSPOT_ACCESS_TOKEN missing from environment/.env")
        self.runs = runs_repo or RunsRepo()
        self.current_run_id: UUID | None = None
        self._http = httpx.Client(
            timeout=30, headers={"Authorization": f"Bearer {self.token}"}
        )

    def _archive(self, op: str, request: dict[str, Any], response: Any) -> None:
        try:
            self.runs.archive_payload(
                source=self.SOURCE,
                response=response,
                request={"op": op, **request},
                source_run_id=self.current_run_id,
            )
        except Exception:
            pass

    @_retry
    def find_contact(
        self, email: str | None = None, name: str | None = None
    ) -> dict[str, Any] | None:
        """First matching CRM contact by email (exact) or name (contains)."""
        filters: list[dict[str, Any]] = []
        if email:
            filters = [{"propertyName": "email", "operator": "EQ", "value": email}]
        elif name:
            parts = name.strip().split()
            if len(parts) >= 2:
                filters = [
                    {"propertyName": "firstname", "operator": "EQ", "value": parts[0]},
                    {"propertyName": "lastname", "operator": "EQ", "value": parts[-1]},
                ]
            else:
                filters = [{"propertyName": "lastname", "operator": "EQ", "value": name}]
        if not filters:
            return None
        body = {
            "filterGroups": [{"filters": filters}],
            "properties": ["email", "firstname", "lastname", "company", "associatedcompanyid"],
            "limit": 2,
        }
        resp = self._http.post(f"{BASE}/crm/v3/objects/contacts/search", json=body)
        resp.raise_for_status()
        data = resp.json()
        self._archive("find_contact", {"email": email, "name": name}, data)
        results = data.get("results") or []
        return results[0] if results else None

    DEAL_PROPERTIES = [
        "dealname", "dealstage", "pipeline", "amount", "closedate",
        "hs_lastmodifieddate", "notes_last_updated", "hs_next_step", "hubspot_owner_id",
    ]

    @_retry
    def _search_deals(self, filters: list[dict[str, Any]], limit: int, op: str,
                      sorts: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "filterGroups": [{"filters": filters}],
            "properties": self.DEAL_PROPERTIES,
            "limit": min(limit, 100),
        }
        if sorts:
            body["sorts"] = sorts
        resp = self._http.post(f"{BASE}/crm/v3/objects/deals/search", json=body)
        resp.raise_for_status()
        data = resp.json()
        self._archive(op, {"filters": filters}, data)
        return data.get("results") or []

    def open_deals(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._search_deals(
            [{"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"}],
            limit, op="open_deals",
        )

    def won_deals(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._search_deals(
            [{"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "true"}],
            limit, op="won_deals",
            sorts=[{"propertyName": "closedate", "direction": "DESCENDING"}],
        )

    @_retry
    def deal_associations(self, deal_id: str, to_type: str) -> list[str]:
        """Associated object ids for a deal (to_type: companies | contacts)."""
        resp = self._http.get(f"{BASE}/crm/v4/objects/deals/{deal_id}/associations/{to_type}")
        resp.raise_for_status()
        data = resp.json()
        self._archive("deal_associations", {"deal_id": deal_id, "to": to_type}, data)
        return [str(r["toObjectId"]) for r in data.get("results") or []]

    @_retry
    def get_company(self, company_id: str) -> dict[str, Any] | None:
        resp = self._http.get(
            f"{BASE}/crm/v3/objects/companies/{company_id}",
            params={"properties": "name,domain"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        self._archive("get_company", {"company_id": company_id}, data)
        return data

    @_retry
    def get_contact(self, contact_id: str) -> dict[str, Any] | None:
        resp = self._http.get(
            f"{BASE}/crm/v3/objects/contacts/{contact_id}",
            params={"properties": "email,firstname,lastname,jobtitle"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        self._archive("get_contact", {"contact_id": contact_id}, data)
        return data

    @_retry
    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        """Meeting engagement + associated contact ids (meeting_brief input)."""
        resp = self._http.get(
            f"{BASE}/crm/v3/objects/meetings/{meeting_id}",
            params={"properties": "hs_meeting_title,hs_meeting_start_time",
                    "associations": "contacts"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        self._archive("get_meeting", {"meeting_id": meeting_id}, data)
        return data
