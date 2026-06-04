---
workflow: 02_evaluate_prospects
objective: Process raw filing data to detect operational pain points and portfolio complexity indicators.
inputs:
  - Ingested filings (data/filings/) and RAW entities (db/memory.db)
  - Clarion qualification rules (encoded below)
outputs:
  - Observations attached to entities
  - Entities transitioned to status QUALIFIED (or REJECTED)
tools:
  - tools/sec_parser.py
  - tools/db_client.py
tier: 1   # local CPU + DB only; no spend, no outbound
---

# Workflow 02 — Evaluate Prospects

## Execution steps
1. **(EDGAR-native)** The agent MUST inspect each downloaded **Form D** record
   (`data/filings/formd_*.json`). If `is_pooled_investment` AND `is_new` (a fresh
   fund vehicle), the agent MUST log a `greenfield_launch` observation. The agent
   SHOULD record `industry_group` / issuer text as a `strategy` observation.
2. **(EDGAR-native)** The agent MUST parse the downloaded **13F XML** files. If
   option positions (Puts/Calls) represent **>15%** of total holdings value, the
   agent MUST log a `derivatives_complex` observation
   (`tools/sec_parser.py --13f <file>`).
3. **(external ADV feed — optional)** Form ADV is **not** on EDGAR, so this step
   runs only if an external ADV record is supplied. When available, the agent
   MUST check Schedule D §7.B.(1) Q.23 for an audit opinion flagged
   "Report Not Yet Received" past the **120-day** window
   (`tools/sec_parser.py --adv <file> --what audit`) and, if flagged, log an
   `audit_delay` observation.
   > This detection is defensive — it fires only when the audit-status field is
   > confidently located. A parser `skip` means "no signal", never a positive.
4. When an external ADV record is available, the agent MAY extract the
   **COO / CCO / CIO** from Schedules A & B
   (`tools/sec_parser.py --adv <file> --what executives`) and record each as a
   `contact` observation (`key_fact` = role, `value` = name).
5. If an entity exhibits **any** qualifying signal (`derivatives_complex`,
   `greenfield_launch`, or `audit_delay`) **alongside** a relevant strategy tag,
   the agent MUST update its status to `QUALIFIED`. Otherwise the agent SHOULD
   leave it `RAW` or mark `REJECTED`.
6. The agent MUST `log_execution` the outcome for each entity evaluated.

## Qualification rule (summary)
`QUALIFIED  ⟺  (derivatives_complex OR greenfield_launch OR audit_delay) AND strategy_fit`
(`audit_delay` is available only when an external ADV feed is supplied.)

## Error handling
- A parser `skip` (unparseable/missing field) MUST NOT fail the entity; record it
  and continue.
- A `fatal` (corrupt DB, bad input contract) MUST halt and surface for review.

## Learnings
<!-- self-healing loop notes, newest first -->
