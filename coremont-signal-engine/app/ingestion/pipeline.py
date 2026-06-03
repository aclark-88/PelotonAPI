"""Daily pipeline orchestrator (Jobs 1-5).

Run via ``python -m app.cli ingest`` (live SEC) or ``--seed`` (offline sample).
Kept deliberately linear and side-effect-light so it's easy to schedule (cron,
GitHub Actions, or any task runner) and easy for Claude Code to maintain.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import config
from ..db import session_scope
from .edgar_client import EdgarClient, FormDRecord, parse_form_d_xml
from . import export_job, formd, signal_job


def _record_from_seed(d: dict) -> FormDRecord:
    import datetime as dt

    def _date(s):
        return dt.date.fromisoformat(s) if s else None

    return FormDRecord(
        accession_no=d.get("accession_no", ""),
        cik=d.get("cik", ""),
        issuer_name=d["issuer_name"],
        jurisdiction=d.get("jurisdiction"),
        entity_type=d.get("entity_type"),
        hq_city=d.get("hq_city"),
        hq_state=d.get("hq_state"),
        filing_date=_date(d.get("filing_date")),
        first_sale_date=_date(d.get("first_sale_date")),
        is_amendment=d.get("is_amendment", False),
        industry_group=d.get("industry_group"),
        investment_fund_type=d.get("investment_fund_type"),
        offering_amount=d.get("offering_amount"),
        amount_sold=d.get("amount_sold"),
        remaining_amount=d.get("remaining_amount"),
        exemptions=d.get("exemptions", []),
        related_persons=d.get("related_persons", []),
        raw_payload=d,
    )


def load_seed_records(path: str | Path | None = None) -> list[FormDRecord]:
    path = Path(path or (config.BASE_DIR / "seed" / "sample_formd.json"))
    data = json.loads(Path(path).read_text())
    return [_record_from_seed(d) for d in data]


def fetch_live_records(lookback_days: int | None = None) -> list[FormDRecord]:
    lookback = lookback_days or config.ingest_lookback_days()
    with EdgarClient() as client:
        return client.fetch_recent_form_d(lookback)


def run_pipeline(*, seed: bool = False, lookback_days: int | None = None, export_min_tier: int = 2) -> dict:
    """Execute Jobs 1-5 end-to-end inside one transaction."""
    if seed:
        records = load_seed_records()
        source = "seed"
    else:
        records = fetch_live_records(lookback_days)
        source = "live"

    with session_scope() as session:
        ingest_stats = formd.persist_records(session, records)   # Job 1 + Job 2
        # Job 3 (adviser) runs inside Job 4 per-manager.
        signal_stats = signal_job.run(session)                   # Job 4
        csv_path = export_job.write_csv(session, min_tier=export_min_tier)  # Job 5
        rows = export_job.build_rows(session, min_tier=export_min_tier)
        crm_stats = export_job.push_to_hubspot(rows)

    return {
        "source": source,
        "ingest": ingest_stats,
        "signals": signal_stats,
        "export_csv": csv_path,
        "export_rows": len(rows),
        "crm": crm_stats,
    }
