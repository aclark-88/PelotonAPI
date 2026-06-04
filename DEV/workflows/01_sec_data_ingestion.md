---
workflow: 01_sec_data_ingestion
objective: Ingest Form D, Form ADV, and Form 13F metadata and raw filings.
inputs:
  - API_KEY            # SEC_API_KEY (from .env, never hardcoded)
  - date_range         # {from: YYYY-MM-DD, to: YYYY-MM-DD}
  - strategy_keywords  # e.g. ["Hedge Fund", "Private Offering"]
outputs:
  - Raw filing XML/HTML documents downloaded to data/filings/
  - Filing metadata persisted to db/memory.db (entities @ status RAW)
tools:
  - tools/sec_downloader.py
  - tools/db_client.py
tier: 2   # network reads + paid API spend; bounded by the budget guard
---

# Workflow 01 — SEC Data Ingestion

## Preconditions
- The agent MUST verify `SEC_API_KEY` is present in the environment before any
  call. If absent, the agent MUST halt this workflow and report a `fatal`.
- The agent MUST confirm the paid-call budget (`SEC_API_BUDGET_CALLS`) is set and
  sufficient for the intended `date_range`. The agent MUST NOT launch a
  high-volume run without this budget verification (cost-safety boundary).

## Execution steps
1. The agent MUST query the SEC Query API (via `tools/sec_downloader.py query`)
   to identify **Form D** filings containing strategy keywords matching
   "Hedge Fund" and "Private Offering" within the target `date_range`.
2. The agent MUST search for **Form ADV** annual updates and identify funds
   listed in Schedule D, Section 7.B.(1).
3. The agent MUST download the latest **13F-HR** filings for target managers,
   retrieving the XML information tables into `data/filings/`.
4. The parser MUST use high-performance `lxml` methods
   (`tools/sec_parser.py parse_13f_infotable`) to extract holdings data.
5. For each discovered manager, the agent SHOULD `upsert_entity` into
   `db/memory.db` at status `RAW`, recording `crd`, `cik`, `firm_name`, and the
   raw `strategies` text for downstream evaluation.
6. The agent MUST `log_execution` a trace row for each step (success/retry/skip/
   fatal) so the self-healing loop has an audit trail.

## Error handling
- If an API **rate limit (429)** is hit, the tool returns `retry` with a
  `retry_after`; the agent MUST pause for that window and re-queue the item.
- If a filing **fails to parse**, the agent MUST log the error as `skip` and move
  to the next item in the queue — it MUST NOT abort the whole run.
- On `fatal` (bad credentials / budget exhausted), the agent MUST stop and
  surface the condition for human attention.

## Learnings
<!-- The self-healing loop appends dated notes here when it adjusts a tool or
     parameter and verifies the fix. Keep newest first. -->
