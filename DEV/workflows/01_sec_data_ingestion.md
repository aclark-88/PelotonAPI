---
workflow: 01_sec_data_ingestion
objective: Ingest Form D and Form 13F metadata and raw filings from SEC EDGAR.
inputs:
  - EDGAR_IDENTITY     # "Name email" fair-access User-Agent (from .env)
  - date_range         # {from: YYYY-MM-DD, to: YYYY-MM-DD}
  - strategy_keywords  # e.g. ["Hedge Fund", "Private Offering"]
outputs:
  - Raw 13F information-table XML + normalized Form D JSON in data/filings/
  - Filing metadata persisted to db/memory.db (entities @ status RAW)
tools:
  - tools/sec_downloader.py   # edgartools / free EDGAR backend
  - tools/db_client.py
tier: 2   # network reads (free EDGAR, fair-access rate limited)
---

# Workflow 01 — SEC Data Ingestion (edgartools / free EDGAR)

## Preconditions
- The agent MUST verify `EDGAR_IDENTITY` is set before any call. SEC fair access
  requires a "Name email" User-Agent. If absent, the tool returns `fatal`.
- The agent SHOULD confirm `EDGAR_MAX_FILINGS` bounds the intended `date_range`
  so a wide range cannot trigger a runaway pull (volume-safety boundary). EDGAR
  is free — there is no per-call spend.

## Execution steps
1. The agent MUST query EDGAR for **Form D** filings over the target
   `date_range` (`tools/sec_downloader.py query --form D ...`). To narrow by
   strategy keywords ("Hedge Fund", "Private Offering"), the agent MAY use the
   full-text search path (`... search --query "Hedge Fund" --form D ...`).
2. The agent MUST download each candidate Form D
   (`... download --accession <acc> --kind formd`), which normalizes issuer,
   `industry_group`, `is_pooled_investment`, `is_new`, first-sale date, and
   offering amounts. A new pooled-investment vehicle sets `greenfield_launch`.
   > Form ADV is **not** hosted on EDGAR (it is an IARD filing); edgartools
   > cannot supply it. The `audit_delay` signal therefore depends on an external
   > ADV feed (see workflow 02) and is NOT produced by this EDGAR step.
3. The agent MUST download the latest **13F-HR** filings for target managers
   (`... download --accession <acc> --kind 13f`), persisting the raw XML
   information table to `data/filings/`.
4. The parser MUST use high-performance `lxml` methods
   (`tools/sec_parser.py parse_13f_infotable`) to extract holdings from that XML.
5. For each discovered manager, the agent SHOULD `upsert_entity` into
   `db/memory.db` at status `RAW` (crd/cik/firm_name/strategies). Where no CRD is
   available from EDGAR, the agent MAY use the issuer CIK as the natural key.
6. The agent MUST `log_execution` a trace row for each step.

## Error handling
- On EDGAR **rate limiting (429)** the tool returns `retry`; the agent MUST pause
  and re-queue the item (edgartools also throttles internally).
- If a filing **fails to parse**, the agent MUST log `skip` and continue.
- On `fatal` (missing identity, forbidden, unexpected error), the agent MUST stop
  and surface the condition.

## Learnings
<!-- The self-healing loop appends dated notes here when it adjusts a tool or
     parameter and verifies the fix. Keep newest first. -->
- 2026-06-03 — Backend migrated from paid api.sec-api.io to edgartools (free
  EDGAR). Form ADV ingestion dropped (ADV is on IARD, not EDGAR); EDGAR-native
  signals are `derivatives_complex` (13F) and `greenfield_launch` (new Form D).
