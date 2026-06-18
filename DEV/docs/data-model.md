# Clarion GTM data model

The Supabase (Postgres 17) operational store for the prospecting engine. It
sits between the raw sources (edgar.tools, Apollo, web search, LinkedIn Sales
Navigator, HeyReach) and the action layer (HubSpot, Apollo sequences, HeyReach
campaigns), and it is the **source of truth** for fund entities, people,
signals, and outreach state.

## Philosophy: signals are the bus, funds are the entities, runs are the audit trail

- **Signals are the bus.** Every observable event — a Form D filing, a COO
  hire, a job post implying PMS displacement — lands in `signals` as one
  normalized row: a `signal_type` (lookup, not enum), a `source` string, the
  source's natural record id, and a `payload` jsonb with the event data.
  Downstream logic (scoring, campaign triggering) consumes signals; it never
  cares which source produced them. Idempotency is structural: `dedupe_key` is
  a generated md5 of `source:source_record_id:signal_type` with a unique
  index, so re-ingesting the same record is a no-op.
- **Funds are the entities.** `funds` and `people` are the canonical records
  everything hangs off. Identity is enforced with partial unique indexes
  (CRD, LEI, domain; LinkedIn URL, Apollo id) scoped to live rows.
  `employment_history` is append-only job history — every observed job is a
  row, closed but never overwritten — powering "champion moved" alerts.
- **Runs are the audit trail.** Every orchestration run writes a `source_runs`
  row, and every row it produces anywhere carries that `source_run_id`.
  "Where did this fund come from?" is one join. Raw API responses are archived
  append-only in `raw_payloads` (deduped by content hash), so parsers can be
  re-run against history without re-hitting APIs.

Scores follow the same append-only discipline: `scoring_runs` keeps every
recomputation with model version, inputs, and reasoning; `funds.fit_score` /
`tier` are merely caches of the latest.

## ER diagram

```mermaid
erDiagram
    source_runs ||--o{ raw_payloads : "archives"
    source_runs ||--o{ signals : "produced (source_run_id on every table)"

    signal_types ||--o{ signals : "classifies"
    signal_types ||--o{ campaigns : "targets"
    strategy_types ||--o{ funds : "validates strategies[]"

    funds ||--o{ funds : "parent_fund_id (spinout)"
    funds ||--o{ people : "current_fund_id"
    funds ||--o{ employment_history : ""
    people ||--o{ employment_history : ""
    funds ||--o{ signals : ""
    people ||--o{ signals : ""
    signals ||--o{ signals : "superseded_by"

    people ||--o{ drafts : ""
    people ||--o{ outreach_attempts : ""
    campaigns ||--o{ drafts : ""
    campaigns ||--o{ outreach_attempts : ""
    signals ||--o{ drafts : "triggered"
    signals ||--o{ outreach_attempts : "triggered"
    drafts |o--o| outreach_attempts : "draft_id / sent_attempt_id"
    outreach_attempts ||--o{ replies : ""

    funds ||--o{ fund_summaries : "embedding per fund"

    funds {
        uuid id PK
        text legal_name
        text crd UK
        text lei UK
        citext primary_domain UK
        numeric aum_usd_millions
        text aum_band "generated"
        text_array strategies
        uuid parent_fund_id FK
        int fit_score "cache"
        smallint tier "cache"
        jsonb metadata
    }
    people {
        uuid id PK
        text full_name
        citext email
        text linkedin_url UK
        text apollo_id UK
        uuid current_fund_id FK
        text current_role
        enum current_role_seniority
        enum current_role_function
        bool is_buying_committee_member "trigger"
    }
    signals {
        uuid id PK
        uuid fund_id FK "nullable"
        uuid person_id FK "nullable"
        text signal_type FK
        text source
        text source_record_id
        text dedupe_key UK "generated md5"
        timestamptz observed_at
        enum urgency
        jsonb payload
        uuid superseded_by FK
    }
    scoring_runs {
        uuid id PK
        text entity_type "fund-signal-person"
        uuid entity_id "polymorphic"
        text model_version
        numeric score
        jsonb inputs
    }
    hubspot_sync {
        uuid id PK
        text entity_type
        uuid local_id "polymorphic"
        text hubspot_object_type
        text hubspot_id
        enum sync_status
    }
```

(`scoring_runs` and `hubspot_sync` key polymorphically on `(entity_type,
entity_id/local_id)` — no FK lines by design.)

## Table inventory

| Group | Tables |
|---|---|
| Lookups (grow by INSERT) | `signal_types`, `strategy_types` |
| Provenance | `source_runs`, `raw_payloads` |
| Identity | `funds`, `people`, `employment_history` |
| Signal layer | `signals`, `scoring_runs` |
| Outreach | `campaigns`, `drafts`, `outreach_attempts`, `replies` |
| CRM sync | `hubspot_sync` |
| Search | `fund_summaries` (+ HNSW on `drafts`/`replies`/`fund_summaries` embeddings, trigram GIN on names) |

Conventions on every table: uuid-v7 PK, `created_at` / `updated_at`
(shared trigger), `created_by`, `source_run_id`, `deleted_at` (soft delete —
**never hard delete**), `metadata jsonb` for source-specific extras. FKs are
`ON DELETE RESTRICT` except the two intentional `SET NULL`s
(`funds.parent_fund_id`, `signals.superseded_by`). RLS is enabled everywhere
with a `service_role`-only policy — defense in depth; the Python client uses
the service-role key.

## The one important function

`fn_observe_job_change(person, new_fund, new_role, observed_at, function,
seniority, source, source_run_id)` — atomically closes the person's open
`employment_history` rows, inserts the new job, updates `people.current_*`
(which re-fires the buying-committee trigger), and emits a `new_role` signal.
The signal's `source_record_id` is deterministic (`person:fund:date`), so
re-observing the same move dedupes instead of double-alerting. Call it via
`PeopleRepo.observe_job_change(...)`; never replicate the steps by hand.

## How to add a new data source — zero migrations

Adding, say, a podcast-transcript scanner:

1. **Pick a `source` string** (`podcast_scan`). It's just text on
   `signals.source` and `raw_payloads.source`. No registry, no DDL.
2. **Add signal types if the source observes new kinds of events** — a row in
   `signal_types` via `gtm/db/seed.py` + `make db.seed` (or a one-line
   `INSERT`). Existing types need nothing.
3. **In the skill**: `RunsRepo.start_run("podcast_scan")` → archive each raw
   response with `archive_payload(...)` → normalize events into
   `SignalsRepo.record_signal(SignalIn(...), source_run_id=...)` →
   `finish_run(...)`. Dedupe, provenance, and urgency defaults are handled by
   the schema.
4. **Source-specific fields** (episode URL, timestamp) go in
   `signals.payload` / `metadata` jsonb. Promote a key to a real column only
   once you need to index or join on it — that's the only case that ever
   needs a migration.

The same recipe applies to new scoring models (a new `model_version` string in
`scoring_runs`) and new strategies (a row in `strategy_types`).

## Python access layer

```
gtm/
├─ models/          # Pydantic v2 mirrors of every table (In/Out pairs)
└─ db/
   ├─ client.py     # singleton; SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from .env
   ├─ seed.py       # signal_types vocabulary (idempotent upsert)
   ├─ repositories/ # funds, people, signals, outreach, runs — the ONLY layer
   │                # that touches the client; orchestration imports repos
   └─ tests/        # pytest integration tests (skip if no credentials)
```

Lifecycle commands live in the top-level `Makefile`: `db.migrate`, `db.seed`,
`db.reset` (local only), `db.test`, `db.types`.
