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
1. The agent MUST check Form ADV **Schedule D, Section 7.B.(1), Question 23(h)**
   to identify whether a fund has flagged its audit opinion as
   "Report Not Yet Received" beyond the standard **120-day** distribution window.
   If flagged, the agent MUST log an `audit_delay` observation
   (`tools/sec_parser.py --adv <file> --what audit`).
   > NOTE: This detection is defensive — it only fires when the audit-status
   > field is confidently located. If the field is absent/ambiguous the parser
   > returns `skip`; the agent MUST treat a `skip` as "no signal", never as a
   > positive.
2. The agent MUST parse Form ADV Part 2A to identify strategy descriptors
   matching **credit, fixed income, relative value, macro, or multi-strategy**,
   and record them as a `strategy` observation.
3. The agent MUST parse the downloaded **13F XML** files. If option positions
   (Puts/Calls) represent **>15%** of total holdings value, the agent MUST log a
   `derivatives_complex` observation (`tools/sec_parser.py --13f <file>`).
4. The agent MUST extract the names of the **Chief Operating Officer, Chief
   Compliance Officer, and Chief Investment Officer** from Schedules A and B
   (`tools/sec_parser.py --adv <file> --what executives`) and record each as a
   `contact` observation (`key_fact` = role, `value` = name).
5. If an entity exhibits **either** an `audit_delay` **or** `derivatives_complex`
   signal **alongside** a relevant strategy tag, the agent MUST update its status
   to `QUALIFIED`. Otherwise the agent SHOULD leave it `RAW` or mark `REJECTED`.
6. The agent MUST `log_execution` the outcome for each entity evaluated.

## Qualification rule (summary)
`QUALIFIED  ⟺  (audit_delay OR derivatives_complex) AND strategy_fit`

## Error handling
- A parser `skip` (unparseable/missing field) MUST NOT fail the entity; record it
  and continue.
- A `fatal` (corrupt DB, bad input contract) MUST halt and surface for review.

## Learnings
<!-- self-healing loop notes, newest first -->
