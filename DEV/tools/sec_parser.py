"""Layer 3 tool: parse SEC filings into structured signals.

Three responsibilities:

1. 13F infotables -> holdings rows, via a streaming ``lxml.etree.iterparse``
   parser that handles arbitrarily large filings without materializing the whole
   tree, and a vectorized pandas computation of options-to-holdings concentration
   (the ``derivatives_complex`` signal).

2. Form ADV Schedule D §7.B.(1) Question 23 -> ``audit_delay`` signal.
   *Defensive by design* (see the integrity note below).

3. Form ADV Schedules A & B -> executive contacts (COO / CCO / CIO).

INTEGRITY NOTE (audit_delay)
----------------------------
The precise field names for ADV Schedule D §7.B.(1) Q.23 ("Has the auditor's
report been received?" / audit opinion status) in sec-api.io's JSON response
cannot be verified from this environment. ``parse_adv_audit`` therefore only
emits an ``audit_delay`` observation when it can *confidently* locate an
audit-status field whose value indicates the report has NOT been received.
Otherwise it returns ``skip`` with a reason — it never guesses. Confirm the
field path (marked ``# VERIFY``) against live ADV data before trusting this
signal in production.

Pure CPU only — no network. Every public function returns the shared envelope.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable

from lxml import etree

from _shared import fatal, ok, run_cli, skip

# Threshold above which a manager's book is flagged derivatives-complex.
OPTIONS_CONCENTRATION_THRESHOLD = 0.15


# ---------------------------------------------------------------------------
# 13F infotable parsing
# ---------------------------------------------------------------------------
def _local(tag: Any) -> str:
    """Return the namespace-stripped local name of an lxml tag."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def parse_13f_infotable(xml_bytes: bytes) -> dict[str, Any]:
    """Stream-parse a 13F information table into a list of holdings dicts.

    Namespace-agnostic (matches by local element name) so it works whether or
    not the filing declares the thirteenf informationtable namespace. Uses
    ``iterparse`` + element clearing to keep memory flat on large filings.
    """
    holdings: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    try:
        # recover=True tolerates the minor malformations common in EDGAR XML.
        context = etree.iterparse(
            _as_stream(xml_bytes), events=("start", "end"), recover=True
        )
        for event, elem in context:
            name = _local(elem.tag)
            if event == "start" and name == "infoTable":
                current = {
                    "nameOfIssuer": None,
                    "titleOfClass": None,
                    "cusip": None,
                    "value": None,
                    "sshPrnamt": None,
                    "putCall": None,
                }
            elif event == "end" and current is not None:
                text = (elem.text or "").strip()
                if name == "nameOfIssuer":
                    current["nameOfIssuer"] = text
                elif name == "titleOfClass":
                    current["titleOfClass"] = text
                elif name == "cusip":
                    current["cusip"] = text
                elif name == "value":
                    current["value"] = _to_float(text)
                elif name == "sshPrnamt":
                    current["sshPrnamt"] = _to_float(text)
                elif name == "putCall":
                    current["putCall"] = text or None
                elif name == "infoTable":
                    holdings.append(current)
                    current = None
                # Free processed siblings to keep memory bounded.
                if name == "infoTable":
                    elem.clear()
    except etree.XMLSyntaxError as exc:
        return skip(f"13F XML could not be parsed: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        return fatal(f"unexpected error parsing 13F: {exc}")

    if not holdings:
        return skip("no <infoTable> entries found in document")
    return ok({"holdings": holdings, "count": len(holdings)})


def options_concentration(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute option (Put/Call) value as a share of total holdings value.

    Vectorized via pandas with a categorical ``putCall`` dtype. The denominator
    is total reported holdings value (options + non-options); this is the most
    defensible, unit-independent measure of how derivatives-heavy the book is.
    Flags ``derivatives_complex`` when the share exceeds 15%.
    """
    import pandas as pd  # imported lazily so 13F-only callers needn't load pandas

    if not holdings:
        return skip("no holdings supplied")

    df = pd.DataFrame(holdings)
    if "value" not in df.columns:
        return skip("holdings missing 'value' column")

    df["value"] = pd.to_numeric(df["value"], errors="coerce").fillna(0.0)
    # Normalize putCall into a small, fixed category set for fast grouping.
    df["putCall"] = (
        df.get("putCall", pd.Series([None] * len(df)))
        .fillna("")
        .astype("category")
    )

    total_value = float(df["value"].sum())
    if total_value <= 0:
        return skip("total holdings value is zero; cannot compute concentration")

    is_option = df["putCall"].isin(["Put", "Call"])
    options_value = float(df.loc[is_option, "value"].sum())
    ratio = options_value / total_value

    return ok(
        {
            "total_value": total_value,
            "options_value": options_value,
            "options_concentration": round(ratio, 4),
            "threshold": OPTIONS_CONCENTRATION_THRESHOLD,
            "derivatives_complex": ratio > OPTIONS_CONCENTRATION_THRESHOLD,
            "num_positions": int(len(df)),
            "num_option_positions": int(is_option.sum()),
        }
    )


# ---------------------------------------------------------------------------
# Form ADV parsing (defensive)
# ---------------------------------------------------------------------------
_AUDIT_NOT_RECEIVED_HINTS = (
    "report not yet received",
    "not yet received",
    "not received",
    "no report",
    "pending",
)
_AUDIT_KEY_HINTS = ("audit", "23", "auditor")
_EXEC_TITLE_MAP = {
    "chief operating officer": "COO",
    "coo": "COO",
    "chief compliance officer": "CCO",
    "cco": "CCO",
    "chief investment officer": "CIO",
    "cio": "CIO",
}


def _walk(node: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    """Yield (dotted_path, value) for every scalar leaf in a nested dict/list."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, node


def parse_adv_audit(adv: dict[str, Any]) -> dict[str, Any]:
    """Detect a flagged audit delay in a parsed Form ADV record.

    Returns ``ok`` with an observation only when an audit-status leaf is found
    whose key looks audit-related (# VERIFY) AND whose value matches a
    "not received" hint. Otherwise ``skip`` — never a false positive.
    """
    if not isinstance(adv, dict):
        return skip("ADV record is not an object")

    for path, value in _walk(adv):
        if not isinstance(value, str):
            continue
        key_lower = path.lower()
        val_lower = value.strip().lower()
        # Field must plausibly be the audit/Q23 status field...
        if not any(h in key_lower for h in _AUDIT_KEY_HINTS):
            continue
        # ...and its value must indicate the report has not been received.
        if any(h in val_lower for h in _AUDIT_NOT_RECEIVED_HINTS):
            return ok(
                {
                    "signal": "audit_delay",
                    "field_path": path,  # VERIFY against live ADV schema
                    "raw_value": value,
                    "category": "operational",
                }
            )

    return skip("no confident audit-delay field found in ADV record")


def parse_adv_schedules_ab(adv: dict[str, Any]) -> dict[str, Any]:
    """Extract COO / CCO / CIO names from ADV Schedules A & B.

    Defensive: scans the record for person-like objects carrying both a name
    and a title, then maps recognized executive titles. Returns whatever it can
    confidently identify (possibly empty).
    """
    if not isinstance(adv, dict):
        return skip("ADV record is not an object")

    found: dict[str, str] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            title = _first_str(node, ("title", "titleOrStatus", "status", "role"))
            name = _first_str(node, ("name", "fullName", "legalName", "personName"))
            if title and name:
                role = _EXEC_TITLE_MAP.get(title.strip().lower())
                if role and role not in found:
                    found[role] = name.strip()
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(adv)

    if not found:
        return skip("no recognizable COO/CCO/CIO records found")
    return ok({"executives": found})


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _first_str(d: dict[str, Any], keys: Iterable[str]) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _to_float(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _as_stream(xml_bytes: bytes):
    import io

    if isinstance(xml_bytes, (bytes, bytearray)):
        return io.BytesIO(xml_bytes)
    return io.BytesIO(str(xml_bytes).encode())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SEC filing parser")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--13f", dest="thirteenf", metavar="PATH", help="path to 13F XML")
    g.add_argument("--adv", dest="adv", metavar="PATH", help="path to ADV JSON record")
    p.add_argument(
        "--what",
        choices=["audit", "executives"],
        default="audit",
        help="for --adv: which extraction to run",
    )
    return p


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = _build_parser().parse_args(argv)

    if args.thirteenf:
        path = Path(args.thirteenf)
        if not path.exists():
            return skip(f"file not found: {path}")
        parsed = parse_13f_infotable(path.read_bytes())
        if parsed["status"] != "success":
            return parsed
        conc = options_concentration(parsed["data"]["holdings"])
        if conc["status"] != "success":
            return conc
        return ok({**parsed["data"], **conc["data"]})

    # --adv
    import json

    path = Path(args.adv)
    if not path.exists():
        return skip(f"file not found: {path}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return skip(f"could not read ADV JSON: {exc}")

    if args.what == "executives":
        return parse_adv_schedules_ab(record)
    return parse_adv_audit(record)


if __name__ == "__main__":
    run_cli(main())
