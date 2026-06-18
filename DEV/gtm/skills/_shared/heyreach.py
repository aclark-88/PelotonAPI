"""HeyReach client — LinkedIn campaign deployment.

Encodes every hard-won gotcha from _tools/heyreach-cli.md (PATCHED 2026-05-06):
- most reads are POST; only GetById-style calls are GET
- sequence nodes need actionDelay + actionDelayUnit (min 3-hour equivalent)
- CONNECTION_REQUEST payload requires fallbackMessage AND toBeWithdrawnAfterDays
- CONNECTION_REQUEST / MESSAGE require BOTH conditionalNode and unconditionalNode
- DRAFT -> IN_PROGRESS is StartCampaign, never Resume
- 300 req/min per workspace key
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import httpx
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gtm.db.repositories.runs import RunsRepo

BASE = "https://api.heyreach.io/api/public"

_retry = retry(
    retry=retry_if_exception_type((httpx.HTTPError, ConnectionError, TimeoutError)),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)

_END = {"nodeType": "END", "actionDelay": 3, "actionDelayUnit": "HOUR"}


class HeyReachClient:
    SOURCE = "heyreach"

    def __init__(self, runs_repo: RunsRepo | None = None, api_key: str | None = None) -> None:
        load_dotenv(encoding="utf-8-sig")
        self.api_key = api_key or os.environ.get("HEYREACH_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("HEYREACH_API_KEY missing from environment/.env")
        self.runs = runs_repo or RunsRepo()
        self.current_run_id: UUID | None = None
        self._http = httpx.Client(
            timeout=45,
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
        )

    @_retry
    def _call(self, method: str, path: str, body: dict | None = None, op: str = "") -> Any:
        resp = self._http.request(method, f"{BASE}{path}", json=body)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        try:
            self.runs.archive_payload(
                source=self.SOURCE,
                response=data if data else {"status": resp.status_code},
                request={"op": op or path, "method": method, "body": body or {}},
                source_run_id=self.current_run_id,
            )
        except Exception:
            pass
        return data

    # ── senders ──────────────────────────────────────────────────────────────
    def linkedin_accounts(self) -> list[dict[str, Any]]:
        data = self._call(
            "POST", "/li_account/GetAll",
            {"offset": 0, "limit": 50, "keyword": None}, op="li_accounts",
        )
        return data.get("items", [])

    def find_sender(self, keyword: str) -> dict[str, Any] | None:
        keyword = keyword.lower()
        for account in self.linkedin_accounts():
            haystack = " ".join(
                str(account.get(k, "")) for k in ("firstName", "lastName", "emailAddress")
            ).lower()
            if keyword in haystack:
                return account
        return None

    # ── campaigns ────────────────────────────────────────────────────────────
    def create_cr_campaign(
        self,
        name: str,
        linkedin_account_ids: list[int],
        list_id: int,
        cr_note: str | None,
        followup_message: str | None = None,
        followup_delay_days: int = 3,
        withdraw_after_days: int = 30,
        schedule: dict[str, str] | None = None,
        exclude_contacted: bool = True,
        cr_fallback: str = "Hello, I work with launch-stage managers at Coremont. Would value connecting.",
        followup_fallback: str = "Thanks for connecting. Happy to compare notes on launch infrastructure whenever useful.",
    ) -> dict[str, Any]:
        """Campaign: CR -> [MESSAGE on accept] -> END.

        Verified live 2026-06-11: when messages contain {{variables}},
        fallbackMessage is REQUIRED and must itself be variable-free (it is
        what sends when a variable can't resolve — a fallback containing
        {{firstName}} is rejected with 'fallback message is invalid').
        Response key is 'campaignId'. Leads can only be appended AFTER
        StartCampaign ('You cannot add new leads to a draft campaign')."""
        for fallback in (cr_fallback, followup_fallback):
            if "{{" in fallback:
                raise ValueError("HeyReach fallback messages must be variable-free")
        accept_branch: dict[str, Any] = _END
        if followup_message:
            accept_branch = {
                "nodeType": "MESSAGE",
                "actionDelay": followup_delay_days,
                "actionDelayUnit": "DAY",
                "payload": {"messages": [followup_message],
                            "fallbackMessage": followup_fallback},
                "conditionalNode": _END,
                "unconditionalNode": _END,
            }
        sequence = {
            "nodeType": "CONNECTION_REQUEST",
            "actionDelay": 3,
            "actionDelayUnit": "HOUR",
            "payload": {
                "messages": [cr_note] if cr_note else [],
                "fallbackMessage": cr_fallback,
                "toBeWithdrawnAfterDays": withdraw_after_days,
            },
            "conditionalNode": accept_branch,
            "unconditionalNode": _END,
        }
        body = {
            "name": name,
            "linkedInUserListId": list_id,
            "linkedInAccountIds": linkedin_account_ids,
            "excludeContactedFromOtherCampaigns": exclude_contacted,
            "excludeHasOtherAccConversations": False,
            "excludeContactedFromSenderInOtherCampaign": False,
            "excludeListId": None,
            "schedule": schedule
            or {"dailyStartTime": "09:00:00", "dailyEndTime": "17:00:00",
                "timeZoneId": "America/New_York"},
            "sequence": sequence,
        }
        return self._call("POST", "/campaign/Create", body, op="create_campaign")

    def create_empty_list(self, name: str) -> dict[str, Any]:
        return self._call(
            "POST", "/list/CreateEmptyList",
            {"name": name, "listType": "USER_LIST"}, op="create_list",
        )

    def add_leads_to_list(self, list_id: int, leads: list[dict[str, Any]]) -> Any:
        return self._call(
            "POST", "/list/AddLeadsToList", {"listId": list_id, "leads": leads}, op="add_leads"
        )

    def find_campaign(self, name_keyword: str) -> dict[str, Any] | None:
        data = self._call(
            "POST", "/campaign/GetAll",
            {"offset": 0, "limit": 100, "statuses": [], "accountIds": [],
             "keyword": name_keyword}, op="find_campaign",
        )
        items = data.get("items", [])
        return items[0] if items else None

    def add_leads_to_campaign(
        self,
        campaign_id: int,
        linkedin_account_id: int,
        lead: dict[str, Any],
        custom_fields: dict[str, str] | None = None,
    ) -> Any:
        """Append one lead to a (running) campaign with per-lead custom fields
        — the canonical-campaign pattern: one campaign, lead-level copy via
        {{variable}} templates in the sequence."""
        lead_payload = dict(lead)
        if custom_fields:
            lead_payload["customUserFields"] = [
                {"name": k, "value": v} for k, v in custom_fields.items()
            ]
        return self._call(
            "POST", "/campaign/AddLeadsToCampaignV2",
            {"campaignId": campaign_id,
             "accountLeadPairs": [{"linkedInAccountId": linkedin_account_id,
                                   "lead": lead_payload}]},
            op="add_leads_to_campaign",
        )

    def stop_lead_in_campaign(self, campaign_id: int, lead_url: str) -> Any:
        return self._call(
            "POST", "/campaign/StopLeadInCampaign",
            {"campaignId": campaign_id, "leadUrl": lead_url}, op="stop_lead",
        )

    def start_campaign(self, campaign_id: int) -> Any:
        # DRAFT -> IN_PROGRESS. Resume() would 400 on a DRAFT (gotcha #15).
        return self._call(
            "POST", f"/campaign/StartCampaign?campaignId={campaign_id}", op="start_campaign"
        )

    def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        return self._call("GET", f"/campaign/GetById?campaignId={campaign_id}", op="get_campaign")
