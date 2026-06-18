-- 03 · Source provenance
--
-- source_runs   every orchestration run; every row in every other table links
--               back here via source_run_id ("where did this fund come from?")
-- raw_payloads  append-only archive of raw API responses, for parser replay
--               without re-hitting APIs.

create table if not exists public.source_runs (
  id                 uuid primary key default public.uuid_generate_v7(),
  skill_name         text not null,                -- form_d_sweep, spinout_watcher, ...
  started_at         timestamptz not null default now(),
  ended_at           timestamptz,
  status             public.run_status not null default 'running',
  records_processed  integer not null default 0,
  records_inserted   integer not null default 0,
  records_updated    integer not null default 0,
  -- jsonb array-of-objects rather than jsonb[]: identical data, but jsonb[]
  -- round-trips badly through PostgREST / supabase-py.
  errors             jsonb not null default '[]'::jsonb,
  metadata           jsonb not null default '{}'::jsonb,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  created_by         text not null default 'system',
  deleted_at         timestamptz
);

create index if not exists idx_source_runs_skill_started
  on public.source_runs (skill_name, started_at desc);
create index if not exists idx_source_runs_created_at
  on public.source_runs (created_at);

drop trigger if exists trg_source_runs_updated_at on public.source_runs;
create trigger trg_source_runs_updated_at
  before update on public.source_runs
  for each row execute function public.set_updated_at();

create table if not exists public.raw_payloads (
  id             uuid primary key default public.uuid_generate_v7(),
  source         text not null,                    -- edgar_tools, apollo, web_search, ...
  source_run_id  uuid references public.source_runs(id) on delete restrict,
  request        jsonb,
  response       jsonb not null,
  fetched_at     timestamptz not null default now(),
  payload_hash   text not null,                    -- sha256 of canonical response JSON
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  created_by     text not null default 'system',
  deleted_at     timestamptz
);

-- Unpartitioned for now. The table is append-only and BRIN keeps time-range
-- scans cheap; pg_partman is available on this instance and a later,
-- self-contained migration can convert to monthly partitions when volume
-- justifies the operational overhead.
create unique index if not exists uq_raw_payloads_payload_hash
  on public.raw_payloads (payload_hash);
create index if not exists idx_raw_payloads_source_run_id
  on public.raw_payloads (source_run_id);
create index if not exists brin_raw_payloads_fetched_at
  on public.raw_payloads using brin (fetched_at);
create index if not exists idx_raw_payloads_created_at
  on public.raw_payloads (created_at);

drop trigger if exists trg_raw_payloads_updated_at on public.raw_payloads;
create trigger trg_raw_payloads_updated_at
  before update on public.raw_payloads
  for each row execute function public.set_updated_at();
