# Migrations — Clarion GTM data layer

Timestamped SQL migrations for the Supabase (Postgres 17) operational store.
All migrations are idempotent (`create table if not exists`, `create index if
not exists`, `do $$ ... exception when duplicate_object$$` guards), so re-running the
full set against an up-to-date database is a no-op.

## Order and contents

| # | File | What it does |
|---|------|--------------|
| 1 | `20260610100100_extensions_and_helpers.sql` | Enables `citext`, `pg_trgm`, `vector`; defines `uuid_generate_v7()` (PG17 polyfill) and the shared `set_updated_at()` trigger function |
| 2 | `20260610100200_types_and_lookups.sql` | All enum types; `signal_types` + `strategy_types` lookup tables; seeds the initial strategy vocabulary |
| 3 | `20260610100300_provenance.sql` | `source_runs` (orchestration audit trail) and `raw_payloads` (append-only API response archive) |
| 4 | `20260610100400_core_entities.sql` | `funds` (with generated `aum_band`), `people`, `employment_history` |
| 5 | `20260610100500_signals.sql` | `signals` (generated `dedupe_key` + unique index = idempotent ingestion) and `scoring_runs` |
| 6 | `20260610100600_outreach.sql` | `campaigns`, `drafts`, `outreach_attempts`, `replies`; closes the circular `drafts ⇄ outreach_attempts` FK |
| 7 | `20260610100700_crm_sync.sql` | `hubspot_sync` reconciliation ledger |
| 8 | `20260610100800_vector_search.sql` | `fund_summaries`, HNSW indexes on all vector columns, trigram GIN indexes, `match_fund_summaries()` rpc |
| 9 | `20260610100900_functions_triggers_rls.sql` | Buying-committee trigger, strategy validation trigger, `fn_observe_job_change()`, RLS on every table (service_role full access, anon nothing) |
| 10 | `20260611020000_job_change_idempotency.sql` | `fn_observe_job_change` made fully idempotent: re-observing a person's current (fund, role) no longer closes/re-inserts employment rows |

## Applying

With the Supabase CLI (preferred once installed):

```sh
npx supabase link --project-ref taebvsfawhhaujhrcbqe
npx supabase db push          # applies anything not yet in the remote history
```

> **Bootstrap note (2026-06-10):** the remote project was initially migrated via
> the Supabase MCP server (`apply_migration`), which records its own version
> timestamps in `supabase_migrations.schema_migrations`. If you adopt the CLI
> later and `db push` complains about history mismatch, reconcile with
> `npx supabase migration repair` (or `db pull`) rather than re-applying.

## Adding things WITHOUT schema changes

The schema is designed so routine growth is data, not DDL:

- **New signal type** → `insert into signal_types (key, display_name, default_urgency, default_score_weight) values (...)`,
  or add it to `gtm/db/seed.py` and re-run `make db.seed`. Done.
- **New data source** → it's just a new `source` string on `signals` /
  `raw_payloads` rows. Archive raw responses to `raw_payloads`, write normalized
  events to `signals`, link everything to a `source_runs` row. Zero DDL.
- **New strategy** → `insert into strategy_types (key, display_name) values (...)`.
- **New scoring model** → new `model_version` string in `scoring_runs`. History
  is append-only; `funds.fit_score` is only a cache of the latest.
- **Source-specific extra fields** → put them in the row's `metadata` jsonb.
  Promote to a real column only when you need to query or index it.

## When you DO need a migration

- Extending an enum: `alter type public.<enum> add value '<new>'` (additive, safe).
- Promoting a `metadata` key to a column.
- New table for a genuinely new aggregate.
- Converting `raw_payloads` to monthly partitions (pg_partman is available on
  the instance; the table was left unpartitioned at bootstrap with a BRIN index
  on `fetched_at`).

Name new files `YYYYMMDDHHMMSS_short_description.sql`, keep them idempotent,
and never edit an applied migration — always add a new one.
