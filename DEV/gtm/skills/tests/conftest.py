"""Skill-test fixtures.

External APIs are NEVER hit: every source is a fake injected via SourceBundle.
The database is the live linked Supabase project (no local stack exists on
this machine — documented deviation from "clean test schema"); isolation comes
from per-session unique identifiers and soft-delete teardown, the same pattern
as gtm/db/tests.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone

import pytest

from gtm.skills._shared.sources import AdvProfile, FormDRecord, SourceBundle, ThirteenFSnapshot


class FakeEdgar:
    """Configurable in-memory EdgarSource stand-in."""

    def __init__(
        self,
        form_d: list[FormDRecord] | None = None,
        history_counts: dict[str, int] | None = None,
        adv: dict[str, AdvProfile] | None = None,
        thirteen_f: dict[str, list[ThirteenFSnapshot]] | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self.form_d = form_d or []
        self.history_counts = history_counts or {}
        self.adv = adv or {}
        self.thirteen_f = thirteen_f or {}
        self.fail_on = fail_on or set()
        self.current_run_id = None
        self.calls: list[tuple[str, tuple]] = []

    def _check(self, op: str) -> None:
        if op in self.fail_on:
            raise ConnectionError(f"fake edgar failure in {op}")

    def recent_form_d(self, lookback_days: int, max_filings: int = 200) -> list[FormDRecord]:
        self.calls.append(("recent_form_d", (lookback_days,)))
        self._check("recent_form_d")
        return self.form_d[:max_filings]

    def form_d_history_count(self, cik: str) -> int:
        self.calls.append(("form_d_history_count", (cik,)))
        self._check("form_d_history_count")
        return self.history_counts.get(cik, 1)

    def adv_firm_profile(self, crd=None, name=None, cik=None) -> AdvProfile | None:
        self.calls.append(("adv_firm_profile", (crd, cik, name)))
        self._check("adv_firm_profile")
        return self.adv.get(str(crd)) or self.adv.get(str(cik)) or self.adv.get(str(name))

    def thirteen_f_quarters(self, cik: str, quarters: int = 4) -> list[ThirteenFSnapshot]:
        self.calls.append(("thirteen_f_quarters", (cik,)))
        self._check("thirteen_f_quarters")
        return self.thirteen_f.get(cik, [])[:quarters]


class FakeWeb:
    """TavilySearch stand-in: keyed by query substring."""

    def __init__(self, responses: dict[str, list] | None = None, fail: bool = False) -> None:
        self.responses = responses or {}
        self.fail = fail
        self.current_run_id = None
        self.queries: list[str] = []

    def search(self, query: str, **kwargs):
        self.queries.append(query)
        if self.fail:
            raise ConnectionError("fake web search failure")
        for key, results in self.responses.items():
            if key.lower() in query.lower():
                return results
        return []


class FakeApollo:
    def __init__(
        self,
        people_by_domain: dict[str, list] | None = None,
        enrich_result=None,
        fail: bool = False,
    ) -> None:
        self.people_by_domain = people_by_domain or {}
        self.enrich_result = enrich_result
        self.fail = fail
        self.current_run_id = None

    def search_people(self, domain=None, org_name=None, titles=None, per_page=10):
        if self.fail:
            raise ConnectionError("fake apollo failure")
        return self.people_by_domain.get(domain or org_name or "", [])

    def enrich_person(self, **kwargs):
        if self.fail:
            raise ConnectionError("fake apollo failure")
        return self.enrich_result


class FakeHubSpot:
    def __init__(
        self,
        contacts: list[dict] | None = None,
        deals: list[dict] | None = None,
        won: list[dict] | None = None,
        companies: dict[str, dict] | None = None,
        crm_contacts: dict[str, dict] | None = None,
        associations: dict[tuple[str, str], list[str]] | None = None,
        meetings: dict[str, dict] | None = None,
    ) -> None:
        self.contacts = contacts or []
        self.deals = deals or []
        self.won = won or []
        self.companies = companies or {}
        self.crm_contacts = crm_contacts or {}
        self.associations = associations or {}
        self.meetings = meetings or {}
        self.current_run_id = None

    def find_contact(self, email=None, name=None):
        for contact in self.contacts:
            if email and contact.get("email") == email:
                return contact
            if name and contact.get("name") == name:
                return contact
        return None

    def open_deals(self, limit=100):
        return self.deals[:limit]

    def won_deals(self, limit=10):
        return self.won[:limit]

    def deal_associations(self, deal_id, to_type):
        return self.associations.get((str(deal_id), to_type), [])

    def get_company(self, company_id):
        return self.companies.get(str(company_id))

    def get_contact(self, contact_id):
        return self.crm_contacts.get(str(contact_id))

    def get_meeting(self, meeting_id):
        return self.meetings.get(str(meeting_id))


class FakeSlack:
    def __init__(self, fail: bool = False) -> None:
        self.posts: list[str] = []
        self.fail = fail
        self.current_run_id = None

    def post(self, text: str) -> bool:
        if self.fail:
            raise ConnectionError("fake slack failure")
        self.posts.append(text)
        return True


def make_search_result(title: str, url: str, content: str, score: float = 0.9, published_date=None):
    from gtm.skills._shared.web_search import SearchResult

    return SearchResult(
        title=title, url=url, content=content, score=score, published_date=published_date
    )


def make_apollo_person(
    apollo_id: str,
    name: str,
    title: str,
    domain: str | None = None,
    email: str | None = None,
    linkedin_url: str | None = None,
):
    from gtm.skills._shared.apollo import ApolloPerson

    return ApolloPerson(
        apollo_id=apollo_id,
        name=name,
        title=title,
        linkedin_url=linkedin_url,
        email=email,
        organization_domain=domain,
        seniority="c_suite",
    )


class FakeLLM:
    """Returns queued responses; repeats the last one when exhausted."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.current_run_id = None

    def complete(self, system: str, user: str, **kwargs) -> str:
        self.calls.append((system, user))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def usage_snapshot(self) -> dict:
        return {"input_tokens": 1000, "output_tokens": 400}


class FakeEmbedder:
    def __init__(self) -> None:
        self.current_run_id = None
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[(len(t) % 97) / 100.0] * 1536 for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class FakeHeyReach:
    def __init__(self, sender: dict | None = None, fail: bool = False) -> None:
        self.sender = sender if sender is not None else {"id": 42, "firstName": "Alex", "lastName": "Clark"}
        self.fail = fail
        self.lists: list[dict] = []
        self.leads: list[tuple[int, list]] = []
        self.campaigns: list[dict] = []
        self.started: list[int] = []
        self._next_id = 9000
        self.current_run_id = None

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def find_sender(self, keyword: str):
        if self.fail:
            raise ConnectionError("fake heyreach failure")
        return self.sender

    def create_empty_list(self, name: str) -> dict:
        list_id = self._id()
        self.lists.append({"id": list_id, "name": name})
        return {"id": list_id}

    def add_leads_to_list(self, list_id: int, leads: list) -> dict:
        self.leads.append((list_id, leads))
        return {"addedCount": len(leads)}

    def create_cr_campaign(self, **kwargs) -> dict:
        campaign_id = self._id()
        self.campaigns.append({"id": campaign_id, **kwargs})
        return {"campaignId": campaign_id}

    def start_campaign(self, campaign_id: int):
        self.started.append(campaign_id)
        return {}

    def find_campaign(self, name_keyword: str):
        for campaign in self.campaigns:
            if name_keyword.lower() in str(campaign.get("name", "")).lower():
                return campaign
        return None

    def add_leads_to_campaign(self, campaign_id, linkedin_account_id, lead, custom_fields=None):
        if self.fail:
            raise ConnectionError("fake heyreach failure")
        self.added = getattr(self, "added", [])
        self.added.append({"campaign_id": campaign_id, "account_id": linkedin_account_id,
                           "lead": lead, "custom_fields": custom_fields or {}})
        return {"addedLeadsCount": 1, "updatedLeadsCount": 0, "failedLeadsCount": 0}

    def stop_lead_in_campaign(self, campaign_id, lead_url):
        return {}


VALID_CR = (
    "Hi {{firstName}}, our Clarion PMS delivers live multi-asset P&L and intraday "
    "risk across rates, FX, and credit derivatives, the exact mix a global macro "
    "book runs on. Would value connecting to compare notes on consolidated risk "
    "across listed and OTC."
)
VALID_FOLLOWUP = (
    "Thanks for connecting. Clarion runs the consolidated book of record for "
    "several macro launches, live risk included. If intraday Greeks across listed "
    "and OTC is on your roadmap, happy to walk through how we handle it. Open to "
    "15 minutes next week?"
)


@pytest.fixture(scope="session")
def db():
    try:
        from gtm.db.client import get_settings
        from gtm.skills._shared.context import RepoBundle

        settings = get_settings()
        if not (settings.supabase_url and settings.supabase_service_role_key):
            pytest.skip("Supabase credentials not configured")
        return RepoBundle()
    except Exception as exc:
        pytest.skip(f"Supabase not configured: {exc}")


@pytest.fixture(scope="session")
def run_suffix() -> str:
    return uuid.uuid4().hex[:10]


@pytest.fixture()
def fresh_cik() -> str:
    """A unique fake CIK per test so dedupe never collides across sessions."""
    return str(random.randint(8_000_000_000, 8_999_999_999))


@pytest.fixture(scope="session")
def cleanup(db):
    created: list[tuple[str, str]] = []
    yield created
    now = datetime.now(timezone.utc).isoformat()
    for table, row_id in reversed(created):
        try:
            db.client.table(table).update({"deleted_at": now}).eq("id", row_id).execute()
        except Exception:
            pass


def make_sources(**kwargs) -> SourceBundle:
    return SourceBundle(**kwargs)


def make_form_d(
    cik: str,
    issuer: str,
    accession: str | None = None,
    fund_type: str = "Hedge Fund",
    industry_group: str = "Pooled Investment Fund",
    offering: float | None = 100_000_000,
    amendment: bool = False,
) -> FormDRecord:
    return FormDRecord(
        accession=accession or f"TEST-{uuid.uuid4().hex[:12]}",
        cik=cik,
        issuer_name=issuer,
        filed_at=datetime.now(timezone.utc),
        is_amendment=amendment,
        industry_group=industry_group,
        fund_type=fund_type,
        total_offering_usd=offering,
        related_persons=[{"name": "Test Founder", "roles": ["Executive Officer"]}],
    )
