"""Layer 3 tool: synthesize qualified leads into tailored outreach drafts.

Merges a qualified entity's signals + executive contacts with pre-structured
Coremont Clarion marketing copy and writes a clean draft to
``drafts/{firm_name}_outreach.md``.

TIER-4 SAFETY BOUNDARY
----------------------
This tool ONLY drafts to the local filesystem. It MUST NOT send email, contact a
prospect, or write to any CRM. Those are Tier-4 actions requiring explicit human
approval and are intentionally not implemented here.

Returns the shared JSON envelope.
"""

from __future__ import annotations

import argparse
import re
from typing import Any

from _shared import DRAFTS_DIR, PROJECT_ROOT, ensure_dirs, fatal, ok, run_cli, skip
from db_client import get_entity_by_crd

# Signal -> Clarion value-prop copy block. Keys match observation categories /
# key_facts produced by workflow 02.
COPY_BLOCKS: dict[str, str] = {
    "audit_delay": (
        "I noticed signals suggesting your fund's audit timeline may be running "
        "past the standard distribution window. Coremont Clarion gives you a "
        "single consolidated **IBOR/ABOR** book of record, and our middle-office "
        "**Operations Concierge** owns the reconciliations end-to-end — "
        "eliminating the data mismatches that typically stall audit delivery and "
        "compressing your close cycle."
    ),
    "derivatives_complex": (
        "Your book appears to carry meaningful optionality and derivatives "
        "exposure. Clarion's **quantitative library**, **real-time risk**, and "
        "independent **valuation framework** are built precisely for "
        "multi-asset, derivatives-heavy portfolios — giving you consistent "
        "cross-book P&L, Greeks, and exposure views without bolting together "
        "spreadsheets."
    ),
    "greenfield_launch": (
        "Congratulations on the launch. Clarion is an **institutional-grade, "
        "turnkey** PMS + managed middle office — you get day-one operational "
        "infrastructure that would otherwise require a sizeable internal "
        "build-out, letting you stand up with **minimal middle-office "
        "headcount** and scale as the book grows."
    ),
    "aum_growth": (
        "Your book has grown materially quarter-over-quarter. Rapid AUM growth "
        "is exactly where operational scaling pain shows up — reconciliations, "
        "financing, treasury, and cross-book P&L. Clarion's unified PMS plus "
        "Coremont's managed middle office let you **scale without a "
        "proportional middle-office build-out**."
    ),
}

_SIGNAL_PRIORITY = (
    "audit_delay",
    "derivatives_complex",
    "aum_growth",
    "greenfield_launch",
)


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name).strip().lower()
    return re.sub(r"[\s_-]+", "_", s) or "firm"


def _pick_signals(observations: list[dict[str, Any]]) -> list[str]:
    """Return matched copy-block keys, highest priority first, de-duplicated."""
    present = set()
    for obs in observations:
        for field in (obs.get("category"), obs.get("key_fact")):
            if not field:
                continue
            f = str(field).strip().lower()
            for key in COPY_BLOCKS:
                if key in f:
                    present.add(key)
    return [k for k in _SIGNAL_PRIORITY if k in present]


def build_draft(
    entity: dict[str, Any], observations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compose the outreach markdown for one qualified entity (no file I/O)."""
    firm = entity.get("firm_name", "the firm")
    strategies = entity.get("strategies", "")
    signals = _pick_signals(observations)

    if not signals:
        return skip(
            f"no actionable Clarion signal found for {firm}; nothing to draft"
        )

    # Salutation: prefer COO, then CIO, then CCO if captured as observations.
    execs = {
        o["key_fact"].upper(): o["value"]
        for o in observations
        if str(o.get("category", "")).lower() == "contact"
        and o.get("key_fact")
        and o.get("value")
    }
    addressee = execs.get("COO") or execs.get("CIO") or execs.get("CCO")
    greeting = f"Hi {addressee.split()[0]}," if addressee else "Hi there,"

    blocks = "\n\n".join(COPY_BLOCKS[s] for s in signals)
    signal_labels = ", ".join(s.replace("_", " ") for s in signals)

    body = f"""---
firm: {firm}
crd: {entity.get('crd', '')}
strategies: {strategies}
signals: {signal_labels}
status: DRAFT (Tier-4 human review required before any send)
---

# Outreach draft — {firm}

**To:** {addressee or '[identify decision-maker — COO / CIO / CCO]'}
**Re:** Operational infrastructure for {strategies or 'your strategy'}

{greeting}

{blocks}

Coremont Clarion was built inside Brevan Howard and spun out as a unified,
cloud-based PMS plus managed middle-office service — so it's been proven on
exactly the kind of complexity {firm} runs.

Worth a short call to see if it maps to your priorities this quarter?

Best regards,
[Your name] — Coremont

---
*Generated by WAT v2 sales_copilot. NOT sent. Review and personalize before any
outbound contact (Tier-4 boundary).*
"""
    return ok({"firm_name": firm, "signals": signals, "markdown": body})


def write_draft(entity: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Build and persist a draft to drafts/{firm_name}_outreach.md."""
    built = build_draft(entity, observations)
    if built["status"] != "success":
        return built

    ensure_dirs()
    dest = DRAFTS_DIR / f"{_slug(built['data']['firm_name'])}_outreach.md"
    try:
        dest.write_text(built["data"]["markdown"], encoding="utf-8")
    except OSError as exc:
        return fatal(f"failed writing draft {dest}: {exc}")
    return ok(
        {
            "path": str(dest.relative_to(PROJECT_ROOT)),
            "firm_name": built["data"]["firm_name"],
            "signals": built["data"]["signals"],
        }
    )


def draft_for_crd(crd: str) -> dict[str, Any]:
    """Look up a qualified entity by CRD and write its draft."""
    fetched = get_entity_by_crd(crd)
    if fetched["status"] != "success":
        return fetched
    entity = fetched["data"]["entity"]
    if entity.get("status") not in ("QUALIFIED", "OUTREACH_READY"):
        return skip(
            f"entity {crd} is '{entity.get('status')}', not QUALIFIED; skipping draft"
        )
    return write_draft(entity, fetched["data"]["observations"])


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Clarion outreach draft generator")
    p.add_argument("--crd", required=True, help="CRD of a QUALIFIED entity to draft")
    return p


def main(argv: list[str] | None = None) -> dict[str, Any]:
    args = _build_parser().parse_args(argv)
    return draft_for_crd(args.crd)


if __name__ == "__main__":
    run_cli(main())
