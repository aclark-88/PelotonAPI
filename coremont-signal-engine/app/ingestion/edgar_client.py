"""SEC EDGAR client + Form D XML parser.

Discovery has two paths that back each other up:
  * full-text search (efts) over ICP terms — fast and also catches body-only
    matches, but the FTS service throttles and can return partial pages;
  * a daily-index crawl filtered to ICP issuer names — slower but deterministic
    and complete, independent of FTS.
The structured Form D data lives in each filing's ``primary_doc.xml``. SEC
requires a descriptive User-Agent with a contact email and rate-limits to
~10 req/s; we respect that with a small delay and back off on 429/5xx.

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
_MAX_RETRIES = 4      # exponential backoff on SEC throttling / transient errors


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
    # multi-strategy / macro
    "multi-strategy",
    "multistrategy",
    "global macro",
    "systematic macro",
    "discretionary macro",
    # rates / relative value
    "relative value",
    "fixed income relative value",
    "rates trading",
    "interest rate",
    # credit
    "structured credit",
    "opportunistic credit",
    "private credit",
    "direct lending",
    "distressed credit",
    "credit opportunities",
    "alternative credit",
    "high yield",
    "leveraged loan",
    "emerging markets debt",
    # securitized / structured products
    "asset backed",
    "mortgage backed",
    "residential mortgage",
    "commercial mortgage",
    "collateralized loan",
    "securitized products",
    # vol / event / special sits
    "volatility arbitrage",
    "convertible arbitrage",
    "event driven",
    "special situations",
]


def name_matches_icp(name: str) -> bool:
    """Cheap ICP screen on an issuer name, used by the daily-index backup path.

    Most ICP fund vehicles carry the strategy in the name itself ("… Multi-
    Strategy …", "… Global Macro …", "… Relative Value …"), so a taxonomy match
    on the name is a reliable, deterministic filter that needs no full-text
    search service.
    """
    from .. import taxonomy

    return taxonomy.match_text(name or "").positive_weight > 0


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

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        """GET with polite rate-limiting + exponential backoff on throttling.

        SEC returns 429/403 when its ~10 req/s limit is exceeded; transient
        5xx and network blips also happen. Retrying with backoff (honouring any
        ``Retry-After`` header) keeps a pull complete and deterministic instead
        of silently dropping pages — which is what made coverage vary run to run.
        """
        delay = 1.0
        last_exc: Exception | None = None
        for _ in range(_MAX_RETRIES):
            try:
                resp = self._client.get(url, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                time.sleep(delay)
                delay = min(delay * 2, 16.0)
                continue
            finally:
                time.sleep(_RATE_DELAY_S)
            if resp.status_code in (429, 403, 502, 503, 504):
                retry_after = (resp.headers.get("Retry-After") or "").strip()
                wait = float(retry_after) if retry_after.isdigit() else delay
                last_exc = httpx.HTTPStatusError(
                    f"SEC returned {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
                time.sleep(min(wait, 16.0))
                delay = min(delay * 2, 16.0)
                continue
            resp.raise_for_status()
            return resp
        if last_exc:
            raise last_exc
        raise httpx.HTTPError("SEC request failed without a specific error")

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
        resp = self._get(url)
        return self.parse_daily_index(resp.text)

    def fetch_form_d(self, cik: str, accession_no: str, date_filed: str | None = None) -> FormDRecord:
        acc_nodash = accession_no.replace("-", "")
        url = PRIMARY_DOC_URL.format(cik=cik.lstrip("0"), acc_nodash=acc_nodash)
        resp = self._get(url)
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

    def fetch_form_d_by_index(self, days: int) -> list[FormDRecord]:
        """Deterministic *backup* discovery: crawl the daily index over the last
        ``days`` and download only Form Ds whose issuer name matches an ICP
        signal. Independent of EDGAR full-text search, so coverage is stable
        run-to-run even when the FTS service throttles or returns partial pages.
        """
        end = dt.date.today()
        seen: set[str] = set()
        records: list[FormDRecord] = []
        for delta in range(days):
            day = end - dt.timedelta(days=delta)
            if day.weekday() >= 5:  # SEC posts on business days
                continue
            try:
                entries = self.fetch_daily_index(day)
            except httpx.HTTPError:
                continue
            for e in entries:
                if e.accession_no in seen or not name_matches_icp(e.company):
                    continue
                seen.add(e.accession_no)
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

    def search_form_d(
        self, term: str, start: str, end: str, max_results: int = 30
    ) -> list["SearchHit"]:
        """Full-text search Form D filings for a phrase within a date range.

        Paginates the EDGAR FTS API (10 hits/page) up to ``max_results``.
        """
        hits: list[SearchHit] = []
        for offset in range(0, max_results, 10):
            params = {
                "q": f'"{term}"',
                "forms": "D",
                "startdt": start,
                "enddt": end,
                "from": offset,
            }
            resp = self._get(EFTS_SEARCH_URL, params=params)
            page = self.parse_search_hits(resp.json())
            hits.extend(page)
            if len(page) < 10:  # last page
                break
        return hits

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
