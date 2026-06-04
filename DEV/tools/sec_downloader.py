"""Layer 3 tool: communicate with api.sec-api.io.

Responsibilities
----------------
- Authenticate with ``SEC_API_KEY`` (loaded from .env; never hardcoded).
- Issue Query-API searches for Form D / Form ADV / 13F metadata.
- Download raw filing documents (XML/HTML) to ``data/filings/``.
- Implement exponential backoff on HTTP 429, returning a ``retry`` envelope.
- Classify all other HTTP/network failures into ``skip`` or ``fatal``.
- Enforce a per-run paid-call budget so high-volume queries cannot run away.

Every public function returns the shared JSON envelope. Network calls are the
only place real money is spent, so the budget guard lives here.

CLI examples
------------
    python tools/sec_downloader.py query --form D --from 2026-01-01 --to 2026-06-01 \
        --keywords "Hedge Fund" "Private Offering"
    python tools/sec_downloader.py download --url <filing-url> --name acme_formd.xml
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any

import requests

try:  # optional, but recommended — keeps the key out of the shell history
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

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

QUERY_API_URL = "https://api.sec-api.io"
# NOTE: api.sec-api.io exposes dedicated structured endpoints for ADV and 13F.
# The exact paths/response shapes are marked # VERIFY and should be confirmed
# against current sec-api.io docs before relying on them in production.
FORM_ADV_API_URL = "https://api.sec-api.io/form-adv"  # VERIFY
FORM_13F_API_URL = "https://api.sec-api.io/form-13f/holdings"  # VERIFY

MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 2.0
DEFAULT_TIMEOUT = 30

# Module-level paid-call counter for the budget guard.
_CALLS_MADE = 0


def _api_key() -> str | None:
    return os.environ.get("SEC_API_KEY") or None


def _budget_limit() -> int:
    try:
        return int(os.environ.get("SEC_API_BUDGET_CALLS", "250"))
    except ValueError:
        return 250


def _user_agent() -> str:
    return os.environ.get(
        "SEC_API_USER_AGENT", "Coremont Clarion Prospecting (contact unset)"
    )


def _classify_http_error(status_code: int, body: str) -> dict[str, Any]:
    """Map a non-2xx HTTP status onto a result envelope (never raises)."""
    if status_code in (401, 403):
        return fatal(f"auth failure {status_code} from sec-api.io: {body[:200]}")
    if status_code == 404:
        return skip(f"resource not found (404): {body[:200]}")
    if 400 <= status_code < 500:
        # Other client errors (e.g. 400 malformed query) are item-local.
        return skip(f"client error {status_code}: {body[:200]}")
    if status_code >= 500:
        return retry(f"server error {status_code}: {body[:200]}")
    return skip(f"unexpected status {status_code}: {body[:200]}")


def _request(
    method: str,
    url: str,
    *,
    force: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Perform one budgeted HTTP request with 429 exponential backoff.

    Returns a result envelope. On 429 the function transparently retries up to
    ``MAX_RETRIES`` honoring ``Retry-After``; if it still fails it returns a
    ``retry`` envelope so the caller can re-queue the item later.
    """
    global _CALLS_MADE

    key = _api_key()
    if not key:
        return fatal("SEC_API_KEY is not set; refusing to call api.sec-api.io")

    limit = _budget_limit()
    if limit > 0 and _CALLS_MADE >= limit and not force:
        return fatal(
            f"paid-call budget of {limit} reached this run "
            f"(SEC_API_BUDGET_CALLS). Re-run with --force to override."
        )

    headers = {
        "Authorization": key,
        "User-Agent": _user_agent(),
        "Accept": "application/json",
    }
    headers.update(kwargs.pop("headers", {}))

    backoff = BASE_BACKOFF_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        _CALLS_MADE += 1
        try:
            resp = requests.request(
                method, url, headers=headers, timeout=DEFAULT_TIMEOUT, **kwargs
            )
        except requests.Timeout:
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                backoff *= 2
                continue
            return retry(f"timeout after {MAX_RETRIES} attempts contacting {url}")
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                backoff *= 2
                continue
            return fatal(f"network failure contacting {url}: {exc}")

        if resp.status_code == 429:
            # Respect Retry-After when present, else exponential backoff.
            retry_after = resp.headers.get("Retry-After")
            wait = backoff
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = backoff
            if attempt < MAX_RETRIES:
                time.sleep(wait)
                backoff *= 2
                continue
            return retry(
                "rate limited (429) after exhausting retries",
                data={"retry_after": wait},
            )

        if 200 <= resp.status_code < 300:
            ctype = resp.headers.get("Content-Type", "")
            payload: Any = resp.json() if "application/json" in ctype else resp.content
            return ok(payload)

        # Any other status: classify; retry transient 5xx with backoff.
        classified = _classify_http_error(resp.status_code, resp.text)
        if classified["status"] == "retry" and attempt < MAX_RETRIES:
            time.sleep(backoff)
            backoff *= 2
            continue
        return classified

    return retry(f"exhausted {MAX_RETRIES} attempts contacting {url}")


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------
def query_filings(
    form_type: str,
    date_from: str,
    date_to: str,
    keywords: list[str] | None = None,
    *,
    size: int = 50,
    force: bool = False,
) -> dict[str, Any]:
    """Search the Query API for filings of ``form_type`` in a date range.

    Builds a Lucene-style query string. ``keywords`` are OR-ed into the query
    so e.g. ["Hedge Fund", "Private Offering"] widens the net for Form D.
    """
    clauses = [f'formType:"{form_type}"', f"filedAt:[{date_from} TO {date_to}]"]
    if keywords:
        kw = " OR ".join(f'"{k}"' for k in keywords)
        clauses.append(f"({kw})")
    query = " AND ".join(clauses)

    body = {
        "query": query,
        "from": "0",
        "size": str(size),
        "sort": [{"filedAt": {"order": "desc"}}],
    }
    return _request("POST", QUERY_API_URL, json=body, force=force)


def download_filing(url: str, dest_name: str, *, force: bool = False) -> dict[str, Any]:
    """Download a raw filing document into data/filings/<dest_name>."""
    ensure_dirs()
    result = _request("GET", url, force=force)
    if result["status"] != "success":
        return result

    payload = result["data"]
    raw = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
    dest = FILINGS_DIR / dest_name
    try:
        dest.write_bytes(raw)
    except OSError as exc:
        return fatal(f"failed writing {dest}: {exc}")
    rel = dest.relative_to(PROJECT_ROOT)
    return ok({"path": str(rel), "bytes": len(raw)})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="api.sec-api.io downloader")
    p.add_argument("--force", action="store_true", help="override the paid-call budget")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("query", help="search the Query API")
    q.add_argument("--form", required=True, help='form type, e.g. "D", "ADV", "13F-HR"')
    q.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    q.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    q.add_argument("--keywords", nargs="*", default=None)
    q.add_argument("--size", type=int, default=50)

    d = sub.add_parser("download", help="download a filing document")
    d.add_argument("--url", required=True)
    d.add_argument("--name", required=True, help="destination filename")

    return p


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = _build_parser().parse_args(argv)
    if args.cmd == "query":
        return query_filings(
            args.form,
            args.date_from,
            args.date_to,
            args.keywords,
            size=args.size,
            force=args.force,
        )
    if args.cmd == "download":
        return download_filing(args.url, args.name, force=args.force)
    return fatal(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    run_cli(main())
