"""SEC EDGAR client + Form D XML parser.

Discovery uses EDGAR's daily index (which lists every filing of a given form
type for a date); the structured Form D data lives in each filing's
``primary_doc.xml``. SEC requires a descriptive User-Agent with a contact email
and rate-limits to ~10 req/s, which we respect with a small delay.

The XML parser is pure and unit-tested against a real-shaped ``primary_doc.xml``
so it keeps working even when this environment has no outbound SEC access.

Refs: data.sec.gov / EDGAR full-index & Form D (Reg D, Rule 503).
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import httpx

from .. import config

DAILY_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{date}.idx"
)
ARCHIVES_DIR_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/"
PRIMARY_DOC_URL = ARCHIVES_DIR_URL + "primary_doc.xml"

# EDGAR full-text search — lets us target ICP terms instead of pulling every
# Form D. Returns JSON hits with CIK / accession / date / display name.
EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

_RATE_DELAY_S = 0.15  # ~6-7 req/s, comfortably under SEC's limit


@dataclass
class IndexEntry:
    form_type: str
    company: str
    cik: str
    date_filed: str
    accession_no: str  # dashed form, e.g. 0001234567-26-000123


@dataclass
class SearchHit:
    cik: str
    accession_no: str
    date_filed: str | None
    display_name: str = ""


# ICP phrases to search EDGAR for (multi-word phrases match best in full text).
# These target Clarion's multi-strat / macro / rates / credit / structured-credit
# sweet spot rather than the full firehose of Form D issuers.
ICP_SEARCH_TERMS = [
    "structured credit",
    "multi-strategy",
    "global macro",
    "relative value",
    "opportunistic credit",
    "fixed income",
    "asset backed",
    "mortgage backed",
    "collateralized loan",
    "credit opportunities",
    "systematic macro",
    "volatility arbitrage",
]


@dataclass
class FormDRecord:
    """Parsed, structured Form D filing."""

    accession_no: str
    cik: str
    issuer_name: str
    jurisdiction: str | None
    entity_type: str | None
    hq_city: str | None
    hq_state: str | None
    filing_date: dt.date | None
    first_sale_date: dt.date | None
    is_amendment: bool
    industry_group: str | None
    investment_fund_type: str | None
    offering_amount: float | None
    amount_sold: float | None
    remaining_amount: float | None
    exemptions: list[str] = field(default_factory=list)
    related_persons: list[dict] = field(default_factory=list)
    raw_payload: dict = field(default_factory=dict)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_text(node: ET.Element, *path_options: str) -> str | None:
    """Namespace-agnostic search for the first matching descendant text."""
    wanted = {p.lower() for p in path_options}
    for el in node.iter():
        if _strip_ns(el.tag).lower() in wanted and el.text and el.text.strip():
            return el.text.strip()
    return None


def _to_float(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip().replace(",", "")
    if s == "" or s.lower() in {"indefinite", "n/a"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_form_d_xml(xml_text: str, *, accession_no: str = "", date_filed: str | None = None) -> FormDRecord:
    """Parse a Form D ``primary_doc.xml`` into a structured record.

    Tolerant of namespaces and minor schema-version differences.
    """
    root = ET.fromstring(xml_text)

    # Primary issuer block.
    issuer = None
    for el in root.iter():
        if _strip_ns(el.tag).lower() == "primaryissuer":
            issuer = el
            break
    issuer = issuer if issuer is not None else root

    issuer_name = _find_text(issuer, "entityName") or ""
    cik = _find_text(issuer, "cik") or ""
    jurisdiction = _find_text(issuer, "jurisdictionOfInc", "stateOrCountryDescription")
    entity_type = _find_text(issuer, "entityType")
    hq_city = _find_text(issuer, "city")
    hq_state = _find_text(issuer, "stateOrCountry")

    is_amendment = (_find_text(root, "isAmendment") or "false").strip().lower() == "true"
    submission_type = (_find_text(root, "submissionType") or "D").strip()
    if submission_type.upper().endswith("/A"):
        is_amendment = True

    # dateOfFirstSale wraps the date in a <value> child (and may be "yet to occur").
    first_sale = None
    for el in root.iter():
        if _strip_ns(el.tag).lower() == "dateoffirstsale":
            first_sale = _to_date(_find_text(el, "value")) or _to_date((el.text or "").strip())
            break

    # Offering amounts.
    offering_amount = _to_float(_find_text(root, "totalOfferingAmount"))
    amount_sold = _to_float(_find_text(root, "totalAmountSold"))
    remaining = _to_float(_find_text(root, "totalRemaining"))
    if remaining is None and offering_amount is not None and amount_sold is not None:
        remaining = max(offering_amount - amount_sold, 0.0)

    industry_group = _find_text(root, "industryGroupType")
    fund_type = _find_text(root, "investmentFundType")

    # Exemptions (federalExemptionsExclusions / item references).
    exemptions: list[str] = []
    for el in root.iter():
        if _strip_ns(el.tag).lower() == "item" and el.text and el.text.strip():
            exemptions.append(el.text.strip())

    # Related persons.
    related: list[dict] = []
    for el in root.iter():
        if _strip_ns(el.tag).lower() == "relatedpersoninfo":
            first = _find_text(el, "firstName") or ""
            last = _find_text(el, "lastName") or ""
            rels = [
                r.text.strip()
                for r in el.iter()
                if _strip_ns(r.tag).lower() == "relationship" and r.text
            ]
            name = " ".join(p for p in (first, last) if p).strip()
            if name:
                related.append({"name": name, "relationships": rels})

    return FormDRecord(
        accession_no=accession_no,
        cik=cik.lstrip("0") or cik,
        issuer_name=issuer_name,
        jurisdiction=jurisdiction,
        entity_type=entity_type,
        hq_city=hq_city,
        hq_state=hq_state,
        filing_date=_to_date(date_filed),
        first_sale_date=first_sale,
        is_amendment=is_amendment,
        industry_group=industry_group,
        investment_fund_type=fund_type,
        offering_amount=offering_amount,
        amount_sold=amount_sold,
        remaining_amount=remaining,
        exemptions=exemptions,
        related_persons=related,
        raw_payload={
            "submission_type": submission_type,
            "industry_group": industry_group,
            "investment_fund_type": fund_type,
            "jurisdiction": jurisdiction,
            "entity_type": entity_type,
            "related_persons": related,
            "exemptions": exemptions,
        },
    )


class EdgarClient:
    """Thin HTTP client over EDGAR archives. Network access is optional —
    callers that pass parsed records (seed/offline mode) never touch this.
    """

    def __init__(self, user_agent: str | None = None, timeout: float = 20.0):
        # No hard-coded Host header: httpx sets it per request URL, so the same
        # client works for www.sec.gov (archives/index) and efts.sec.gov (search).
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent or config.sec_user_agent(),
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "EdgarClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def parse_daily_index(text: str) -> list[IndexEntry]:
        """Parse a daily ``form.YYYYMMDD.idx`` and return Form D / D/A rows."""
        entries: list[IndexEntry] = []
        started = False
        for line in text.splitlines():
            if line.startswith("---"):
                started = True
                continue
            if not started or not line.strip():
                continue
            # Fixed-ish columns: Form Type | Company | CIK | Date Filed | File Name
            parts = [p for p in line.split("  ") if p.strip()]
            parts = [p.strip() for p in parts]
            if len(parts) < 5:
                continue
            form_type = parts[0]
            if form_type not in ("D", "D/A"):
                continue
            company, cik, date_filed, filename = parts[1], parts[2], parts[3], parts[-1]
            acc = filename.rsplit("/", 1)[-1].replace(".txt", "")
            entries.append(
                IndexEntry(form_type, company, cik, date_filed, acc)
            )
        return entries

    def fetch_daily_index(self, day: dt.date) -> list[IndexEntry]:
        qtr = (day.month - 1) // 3 + 1
        url = DAILY_INDEX_URL.format(year=day.year, qtr=qtr, date=day.strftime("%Y%m%d"))
        resp = self._client.get(url)
        time.sleep(_RATE_DELAY_S)
        resp.raise_for_status()
        return self.parse_daily_index(resp.text)

    def fetch_form_d(self, cik: str, accession_no: str, date_filed: str | None = None) -> FormDRecord:
        acc_nodash = accession_no.replace("-", "")
        url = PRIMARY_DOC_URL.format(cik=cik.lstrip("0"), acc_nodash=acc_nodash)
        resp = self._client.get(url)
        time.sleep(_RATE_DELAY_S)
        resp.raise_for_status()
        return parse_form_d_xml(resp.text, accession_no=accession_no, date_filed=date_filed)

    def fetch_recent_form_d(self, lookback_days: int) -> list[FormDRecord]:
        """Walk the last N days of daily indexes and fetch each Form D doc."""
        records: list[FormDRecord] = []
        today = dt.date.today()
        for delta in range(lookback_days):
            day = today - dt.timedelta(days=delta)
            if day.weekday() >= 5:  # SEC posts on business days
                continue
            try:
                entries = self.fetch_daily_index(day)
            except httpx.HTTPError:
                continue
            for e in entries:
                try:
                    records.append(self.fetch_form_d(e.cik, e.accession_no, e.date_filed))
                except httpx.HTTPError:
                    continue
        return records

    # --- Targeted full-text search (ICP terms) -------------------------------
    @staticmethod
    def parse_search_hits(payload: dict) -> list["SearchHit"]:
        """Parse an EDGAR full-text search JSON response into hits."""
        hits: list[SearchHit] = []
        for h in payload.get("hits", {}).get("hits", []):
            _id = h.get("_id", "")
            accession = _id.split(":", 1)[0]  # "0001234567-26-000123:primary_doc.xml"
            src = h.get("_source", {})
            ciks = src.get("ciks") or src.get("cik") or []
            if isinstance(ciks, str):
                ciks = [ciks]
            cik = (ciks[0].lstrip("0") if ciks else "")
            names = src.get("display_names") or []
            hits.append(
                SearchHit(
                    cik=cik,
                    accession_no=accession,
                    date_filed=src.get("file_date"),
                    display_name=(names[0] if names else ""),
                )
            )
        return hits

    def search_form_d(self, term: str, start: str, end: str) -> list["SearchHit"]:
        """Full-text search Form D filings for a phrase within a date range."""
        params = {"q": f'"{term}"', "forms": "D", "startdt": start, "enddt": end}
        resp = self._client.get(EFTS_SEARCH_URL, params=params)
        time.sleep(_RATE_DELAY_S)
        resp.raise_for_status()
        return self.parse_search_hits(resp.json())

    def fetch_form_d_by_terms(self, terms: list[str], days: int) -> list[FormDRecord]:
        """Search each ICP term over the last ``days`` and fetch matching Form Ds.

        Only filings that mention an ICP term are downloaded, so this surfaces
        relevant managers far faster than scanning every Form D.
        """
        end = dt.date.today()
        start = end - dt.timedelta(days=days)
        seen: set[str] = set()
        records: list[FormDRecord] = []
        for term in terms:
            try:
                hits = self.search_form_d(term, start.isoformat(), end.isoformat())
            except httpx.HTTPError:
                continue
            for hit in hits:
                if not hit.cik or hit.accession_no in seen:
                    continue
                seen.add(hit.accession_no)
                try:
                    records.append(
                        self.fetch_form_d(hit.cik, hit.accession_no, hit.date_filed)
                    )
                except httpx.HTTPError:
                    continue
        return records
