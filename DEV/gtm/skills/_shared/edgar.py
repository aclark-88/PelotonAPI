"""EDGAR + IAPD source.

- Form D / 13F via edgartools (free EDGAR; EDGAR_IDENTITY fair-access UA).
- Form ADV via the public SEC adviserinfo (IAPD) JSON API — ADV is NOT on
  EDGAR; this is the documented external feed.
- Every fetch is rate-limited (configs/rate_limits.yaml; default 8 req/s vs
  the SEC's 10 req/s ceiling), retried with backoff, and archived to
  raw_payloads before parsing, keyed to the current run.

Field extraction from edgartools objects is deliberately defensive
(getattr chains): edgartools' object model moves between versions, and a
missing optional field must degrade to None, not crash a sweep.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gtm.db.repositories.runs import RunsRepo
from gtm.skills._shared.adv_roster import AdvRoster
from gtm.skills._shared.sources import AdvProfile, FormDRecord, ThirteenFSnapshot

_retry = retry(
    retry=retry_if_exception_type((httpx.HTTPError, ConnectionError, TimeoutError)),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)


class _RateLimiter:
    def __init__(self, per_second: float) -> None:
        self.min_interval = 1.0 / per_second
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


def _first(obj: Any, *paths: str) -> Any:
    """Walk dotted attribute paths; first non-None wins."""
    for path in paths:
        node = obj
        ok = True
        for part in path.split("."):
            node = getattr(node, part, None)
            if node is None:
                ok = False
                break
        if ok and node is not None:
            return node
    return None


class EdgarSource:
    SOURCE = "edgar_tools"

    def __init__(
        self,
        runs_repo: RunsRepo | None = None,
        rate_per_second: float = 8.0,
        identity: str | None = None,
    ) -> None:
        load_dotenv(encoding="utf-8-sig")  # tolerate a BOM from Windows editors
        identity = identity or os.environ.get("EDGAR_IDENTITY", "").strip('"')
        if not identity:
            raise RuntimeError("EDGAR_IDENTITY missing — set it in .env (fatal per fair-access policy)")
        import edgar  # deferred: heavy import

        edgar.set_identity(identity)
        self._edgar = edgar
        self.runs = runs_repo or RunsRepo()
        self.roster = AdvRoster()
        self.limiter = _RateLimiter(rate_per_second)
        self.current_run_id: UUID | None = None  # bound by open_run()
        self._http = httpx.Client(timeout=30, headers={"User-Agent": identity})

    # ── provenance ───────────────────────────────────────────────────────────
    def _archive(self, request: dict[str, Any], response: Any) -> None:
        try:
            self.runs.archive_payload(
                source=self.SOURCE,
                response=response,
                request=request,
                source_run_id=self.current_run_id,
            )
        except Exception:
            # archival must never take down a sweep; the parse still proceeds
            pass

    # ── Form D ───────────────────────────────────────────────────────────────
    @_retry
    def recent_form_d(self, lookback_days: int, max_filings: int = 200) -> list[FormDRecord]:
        self.limiter.wait()
        cutoff = date.today() - timedelta(days=lookback_days)
        # Pull the quarter index and date-filter in Python: passing filing_date
        # to edgartools trips a console warning path that breaks on Windows
        # cp1252 terminals, and the index is already in memory anyway.
        filings = self._edgar.get_filings(form="D")
        records: list[FormDRecord] = []
        for filing in filings:
            if len(records) >= max_filings:
                break
            fdate = getattr(filing, "filing_date", None)
            if fdate is None or fdate < cutoff:
                continue
            try:
                records.append(self._extract_form_d(filing))
            except Exception as exc:
                self._archive(
                    {"op": "form_d_extract", "accession": getattr(filing, "accession_no", "?")},
                    {"error": str(exc)},
                )
        return records

    def _extract_form_d(self, filing: Any) -> FormDRecord:
        self.limiter.wait()
        formd = filing.obj()
        offering = getattr(formd, "offering_data", None)
        total_offering = _first(offering, "offering_sales_amounts.total_offering_amount")
        total_sold = _first(offering, "offering_sales_amounts.total_amount_sold")
        investor_count = _first(offering, "investors.total_number_already_invested")

        def _num(value: Any) -> float | None:
            if value is None:
                return None
            text = str(value).replace(",", "").strip()
            if text.lower() in {"indefinite", ""}:
                return None
            try:
                return float(text)
            except ValueError:
                return None

        related = []
        for person in getattr(formd, "related_persons", None) or []:
            related.append(
                {
                    "name": " ".join(
                        str(p)
                        for p in (
                            _first(person, "first_name"),
                            _first(person, "last_name"),
                        )
                        if p
                    )
                    or str(_first(person, "name") or ""),
                    "roles": list(_first(person, "relationships") or []),
                }
            )

        filed = getattr(filing, "filing_date", None)
        if isinstance(filed, date) and not isinstance(filed, datetime):
            filed = datetime(filed.year, filed.month, filed.day, tzinfo=timezone.utc)

        record = FormDRecord(
            accession=str(getattr(filing, "accession_no", "")),
            cik=str(getattr(filing, "cik", "")),
            issuer_name=str(
                _first(formd, "primary_issuer.entity_name") or getattr(filing, "company", "")
            ),
            filed_at=filed or datetime.now(timezone.utc),
            is_amendment="/A" in str(getattr(filing, "form", "D")),
            industry_group=_first(offering, "industry_group.industry_group_type"),
            fund_type=_first(offering, "industry_group.investment_fund_info.investment_fund_type"),
            total_offering_usd=_num(total_offering),
            total_sold_usd=_num(total_sold),
            investor_count=int(investor_count) if investor_count is not None else None,
            related_persons=related,
            state=_first(formd, "primary_issuer.jurisdiction"),
        )
        self._archive({"op": "form_d", "accession": record.accession}, record.model_dump(mode="json"))
        return record

    @_retry
    def form_d_history_count(self, cik: str) -> int:
        """How many original (non-amendment) Form Ds this issuer has ever filed."""
        self.limiter.wait()
        company = self._edgar.Company(int(cik))
        filings = company.get_filings(form=["D", "D/A"])
        count = sum(1 for f in filings if str(getattr(f, "form", "")) == "D")
        self._archive({"op": "form_d_history", "cik": cik}, {"original_form_d_count": count})
        return count

    # ── Form ADV (SEC monthly FOIA roster — off-EDGAR) ───────────────────────
    # The public adviserinfo search API exposes no AUM and the detail endpoint
    # is 403-gated, so ADV data comes from the monthly SEC FOIA firm-roster
    # CSV in data/adv/ (see _shared/adv_roster.py for refresh instructions).
    def adv_firm_profile(
        self,
        crd: str | None = None,
        name: str | None = None,
        cik: str | None = None,
    ) -> AdvProfile | None:
        if not (crd or name or cik):
            return None
        if not self.roster.available:
            # No roster (e.g. cloud runner without the 40MB FOIA CSV): degrade
            # gracefully — ADV enrichment is skipped, callers treat as "not
            # found". Verification still runs on web + name evidence.
            return None
        profile = self.roster.lookup(crd=crd, cik=cik, name=name)
        self._archive(
            {"op": "adv_roster_lookup", "crd": crd, "cik": cik, "name": name},
            profile.model_dump(mode="json") if profile else {"found": False},
        )
        return profile

    # ── 13F ──────────────────────────────────────────────────────────────────
    @_retry
    def thirteen_f_quarters(self, cik: str, quarters: int = 4) -> list[ThirteenFSnapshot]:
        self.limiter.wait()
        company = self._edgar.Company(int(cik))
        filings = company.get_filings(form="13F-HR").head(quarters)
        snapshots: list[ThirteenFSnapshot] = []
        for filing in filings:
            try:
                snapshots.append(self._extract_13f(cik, filing))
            except Exception as exc:
                self._archive(
                    {"op": "13f_extract", "accession": getattr(filing, "accession_no", "?")},
                    {"error": str(exc)},
                )
        return snapshots

    def _extract_13f(self, cik: str, filing: Any) -> ThirteenFSnapshot:
        self.limiter.wait()
        thirteen_f = filing.obj()
        table = getattr(thirteen_f, "infotable", None)
        if table is None or len(table) == 0:
            raise ValueError("empty infotable")
        cols = {c.lower(): c for c in table.columns}
        value_col = cols.get("value")
        putcall_col = cols.get("putcall") or cols.get("put_call")
        issuer_col = cols.get("issuer") or cols.get("nameofissuer") or cols.get("cusip")

        values = table[value_col].fillna(0).astype(float) if value_col else None
        total = float(values.sum()) if values is not None else 0.0
        top10 = (
            float(values.sort_values(ascending=False).head(10).sum()) / total
            if values is not None and total > 0
            else None
        )
        option_count = 0
        if putcall_col:
            # PutCall is a string column; blanks are present for non-option rows
            put_call = table[putcall_col].astype(str).str.strip().str.upper()
            option_count = int(put_call.isin(["PUT", "CALL"]).sum())

        period = str(
            _first(thirteen_f, "report_period", "period_of_report")
            or getattr(filing, "period_of_report", "")
        )
        snapshot = ThirteenFSnapshot(
            cik=cik,
            period=period,
            position_count=int(len(table)),
            total_value_usd=total,
            option_position_count=option_count,
            top10_concentration=top10,
            positions=[str(x) for x in table[issuer_col].tolist()] if issuer_col else [],
        )
        self._archive(
            {"op": "13f", "accession": str(getattr(filing, "accession_no", ""))},
            snapshot.model_dump(mode="json", exclude={"positions"}),
        )
        return snapshot
