"""Layer 3 tool: retrieve SEC filings via the `edgartools` library (free EDGAR).

This replaces the previous paid api.sec-api.io backend with the open-source
``edgartools`` package, which reads directly from the SEC's public EDGAR system.

Responsibilities
----------------
- Set the SEC fair-access identity (User-Agent) from ``EDGAR_IDENTITY``.
- Query Form D / 13F filing metadata over a date range.
- Full-text search EDGAR for strategy keywords.
- Fetch a specific filing by accession number and persist the raw 13F
  information-table XML (so ``tools/sec_parser.py``'s lxml parser runs on it) or
  a normalized Form D record.
- Map all failures onto the shared JSON envelope (retry / skip / fatal).

Important data-source note
--------------------------
EDGAR does NOT host **Form ADV** (that is filed on IARD, not EDGAR). edgartools
therefore cannot supply the ``audit_delay`` signal. The Form ADV path here
returns a ``skip`` explaining the gap; ADV data must be supplied externally and
parsed with ``tools/sec_parser.py --adv``. The two EDGAR-native signals are
``derivatives_complex`` (13F) and ``greenfield_launch`` (new pooled-investment
Form D).

No API key and no per-call spend — EDGAR is free. A volume guard
(``EDGAR_MAX_FILINGS``) bounds large pulls, honoring the "no high-volume run
without verification" safety rule.

CLI examples
------------
    python tools/sec_downloader.py query  --form D --from 2026-03-01 --to 2026-03-07
    python tools/sec_downloader.py search --query "Hedge Fund" --form D \
        --from 2026-01-01 --to 2026-03-01
    python tools/sec_downloader.py download --accession 0001234567-26-000123 --kind 13f
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

try:  # keep the identity out of shell history
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:  # pragma: no cover - dotenv is optional
    pass

import edgar

from _shared import (
    FILINGS_DIR,
    PROJECT_ROOT,
    ensure_dirs,
    fatal,
    ok,
    retry,
    run_cli,
    skip,
)

DEFAULT_MAX_FILINGS = 500


def _ensure_identity() -> dict[str, Any] | None:
    """Set the SEC fair-access identity; fatal if not configured.

    SEC requires a descriptive User-Agent ("Name email") on every request.
    """
    identity = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not identity:
        return fatal(
            "EDGAR_IDENTITY is not set. SEC fair access requires a "
            "'Name email@example.com' identity. Set it in .env."
        )
    edgar.set_identity(identity)
    return None


def _max_filings() -> int:
    try:
        return int(os.environ.get("EDGAR_MAX_FILINGS", str(DEFAULT_MAX_FILINGS)))
    except ValueError:
        return DEFAULT_MAX_FILINGS


def _classify_exception(exc: Exception) -> dict[str, Any]:
    """Best-effort mapping of an edgartools/httpx exception to an envelope."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return retry(f"rate limited by EDGAR (429): {exc}")
    if status in (401, 403):
        return fatal(f"EDGAR access forbidden ({status}); check EDGAR_IDENTITY: {exc}")
    if status == 404:
        return skip(f"filing not found on EDGAR (404): {exc}")
    if isinstance(status, int) and status >= 500:
        return retry(f"EDGAR server error ({status}): {exc}")
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connect", "transport", "network")):
        return retry(f"transient network error contacting EDGAR: {exc}")
    return fatal(f"unexpected error from edgartools: {exc}")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def query_filings(
    form: str, date_from: str, date_to: str, *, limit: int | None = None
) -> dict[str, Any]:
    """Return filing metadata for ``form`` over an inclusive date range.

    Fast: reads the EDGAR index only (no per-filing document fetch).
    """
    if err := _ensure_identity():
        return err
    cap = limit or _max_filings()
    try:
        filings = edgar.get_filings(form=form, filing_date=f"{date_from}:{date_to}")
    except Exception as exc:  # noqa: BLE001 - mapped to envelope
        return _classify_exception(exc)

    if filings is None or len(filings) == 0:
        return ok({"form": form, "count": 0, "filings": []})

    total = len(filings)
    rows: list[dict[str, Any]] = []
    for i in range(min(total, cap)):
        f = filings[i]
        rows.append(
            {
                "form": getattr(f, "form", form),
                "company": str(getattr(f, "company", "")),
                "cik": str(getattr(f, "cik", "")),
                "accession": str(getattr(f, "accession_no", "")),
                "filing_date": str(getattr(f, "filing_date", "")),
            }
        )
    return ok(
        {
            "form": form,
            "count": len(rows),
            "total_available": total,
            "truncated": total > cap,
            "filings": rows,
        }
    )


def search_fulltext(
    query: str,
    *,
    form: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """EDGAR full-text search for strategy keywords (e.g. "Hedge Fund")."""
    if err := _ensure_identity():
        return err
    try:
        result = edgar.search_filings(
            query,
            forms=form,
            start_date=date_from,
            end_date=date_to,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        return _classify_exception(exc)

    rows: list[dict[str, Any]] = []
    try:
        for hit in list(result)[:limit]:
            rows.append(
                {
                    "form": str(getattr(hit, "form", "")),
                    "company": str(getattr(hit, "company", getattr(hit, "display_names", ""))),
                    "cik": str(getattr(hit, "cik", "")),
                    "accession": str(getattr(hit, "accession_no", getattr(hit, "accession_number", ""))),
                    "filing_date": str(getattr(hit, "filing_date", "")),
                }
            )
    except Exception as exc:  # noqa: BLE001 - iteration shape varies by version
        return skip(f"could not iterate search results: {exc}")
    return ok({"query": query, "count": len(rows), "hits": rows})


# ---------------------------------------------------------------------------
# Downloads / normalization
# ---------------------------------------------------------------------------
def _industry_group(od: Any) -> str:
    """Form D industry group can be a nested object; extract a readable type."""
    ig = getattr(od, "industry_group", None) if od else None
    if ig is None:
        return ""
    # edgartools exposes a typed object with an `industry_group_type` attribute.
    return str(getattr(ig, "industry_group_type", ig))


def _normalize_form_d(obj: Any) -> dict[str, Any]:
    issuer = getattr(obj, "primary_issuer", None)
    od = getattr(obj, "offering_data", None)
    is_pooled = bool(getattr(od, "is_pooled_investment", False)) if od else False
    # A Form D notice with submission_type "D" is a NEW offering notice; "D/A" is
    # an amendment to an existing one. edgartools' `is_new` flag is unrelated and
    # unreliable here, so a new pooled vehicle = (submission_type == "D" & pooled).
    submission = str(getattr(obj, "submission_type", "")).upper()
    is_new_notice = submission == "D"
    return {
        "entity_name": str(getattr(issuer, "entity_name", "")) if issuer else "",
        "cik": str(getattr(issuer, "cik", "")) if issuer else "",
        "jurisdiction": str(getattr(issuer, "jurisdiction", "")) if issuer else "",
        "submission_type": submission,
        "industry_group": _industry_group(od),
        "is_pooled_investment": is_pooled,
        "is_new": is_new_notice,
        "date_of_first_sale": str(getattr(od, "date_of_first_sale", "")) if od else "",
        "offering_sales_amounts": str(getattr(od, "offering_sales_amounts", "")) if od else "",
        "num_related_persons": len(getattr(obj, "related_persons", []) or []),
        # A new pooled-investment vehicle is the EDGAR-native "greenfield_launch".
        "greenfield_launch": is_pooled and is_new_notice,
    }


def download_filing(accession: str, kind: str) -> dict[str, Any]:
    """Fetch one filing by accession and persist what the parser needs.

    kind="13f"   -> save raw information-table XML to data/filings/13f_<acc>.xml
    kind="formd" -> save normalized Form D JSON to data/filings/formd_<acc>.json
    """
    if err := _ensure_identity():
        return err
    ensure_dirs()
    safe_acc = accession.replace("/", "_")

    try:
        filing = edgar.get_by_accession_number(accession)
    except Exception as exc:  # noqa: BLE001
        return _classify_exception(exc)
    if filing is None:
        return skip(f"no filing found for accession {accession}")

    try:
        obj = filing.obj()
    except Exception as exc:  # noqa: BLE001
        return skip(f"could not parse filing {accession}: {exc}")

    if kind == "13f":
        xml = getattr(obj, "infotable_xml", None)
        if not xml:
            return skip(f"filing {accession} has no 13F information table")
        dest = FILINGS_DIR / f"13f_{safe_acc}.xml"
        try:
            dest.write_text(xml if isinstance(xml, str) else xml.decode(), encoding="utf-8")
        except OSError as exc:
            return fatal(f"failed writing {dest}: {exc}")
        return ok(
            {
                "kind": "13f",
                "path": str(dest.relative_to(PROJECT_ROOT)),
                "management_company": str(getattr(obj, "management_company_name", "")),
                "total_value": getattr(obj, "total_value", None),
                "total_holdings": getattr(obj, "total_holdings", None),
            }
        )

    if kind == "formd":
        import json

        record = _normalize_form_d(obj)
        dest = FILINGS_DIR / f"formd_{safe_acc}.json"
        try:
            dest.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            return fatal(f"failed writing {dest}: {exc}")
        return ok({"kind": "formd", "path": str(dest.relative_to(PROJECT_ROOT)), **record})

    return skip(f"unknown kind '{kind}' (expected '13f' or 'formd')")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EDGAR downloader (edgartools backend)")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("query", help="list filing metadata over a date range")
    q.add_argument("--form", required=True, help='e.g. "D", "13F-HR"')
    q.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    q.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    q.add_argument("--limit", type=int, default=None)

    s = sub.add_parser("search", help="EDGAR full-text keyword search")
    s.add_argument("--query", required=True)
    s.add_argument("--form", default=None)
    s.add_argument("--from", dest="date_from", default=None)
    s.add_argument("--to", dest="date_to", default=None)
    s.add_argument("--limit", type=int, default=20)

    d = sub.add_parser("download", help="fetch one filing by accession")
    d.add_argument("--accession", required=True)
    d.add_argument("--kind", choices=["13f", "formd"], required=True)

    return p


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = _build_parser().parse_args(argv)
    if args.cmd == "query":
        return query_filings(args.form, args.date_from, args.date_to, limit=args.limit)
    if args.cmd == "search":
        return search_fulltext(
            args.query,
            form=args.form,
            date_from=args.date_from,
            date_to=args.date_to,
            limit=args.limit,
        )
    if args.cmd == "download":
        return download_filing(args.accession, args.kind)
    return fatal(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    run_cli(main())
