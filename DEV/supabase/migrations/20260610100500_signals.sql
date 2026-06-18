-- 05 · Signal bus and scoring audit trail
--
-- signals       every observed event, normalized. Idempotent ingestion via the
--               generated dedupe_key (md5 of source : source_record_id : type)
--               plus a unique index — re-ingesting the same source record is
--               ON CONFLICT DO NOTHING.
-- scoring_runs  append-only score history; funds.fit_score is only a cache.

create table if not exists public.signals (
  id                uuid primary key default public.uuid_generate_v7(),
  fund_id           uuid references public.funds(id) on delete restrict,    -- nullable: a signal can precede fund creation
  person_id         uuid references public.people(id) on delete restrict,
  signal_type       text not null references public.signal_types(key) on delete restrict,
  source            text not null,         -- edgar_tools, apollo, web_search, linkedin_sn, heyreach, manual, ...
  source_record_id  text not null,         -- natural key in the source system; for manual signals generate a uuid
  observed_at       timestamptz not null,  -- when the event happened
  ingested_at       timestamptz not null default now(),  -- when we saw it
  urgency           public.urgency_level not null default 'this_month',
  urgency_score     integer check (urgency_score between 0 and 100),
  payload           jsonb not null,        -- normalized event data
  dedupe_key        text generated always as (
                      md5(source || ':' || source_record_id || ':' || signal_type)
                    ) stored,
  superseded_by     uuid references public.signals(id) on delete set null,
  metadata          jsonb not null default '{}'::jsonb,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  created_by        text not null default 'system',
  source_run_id     uuid references public.source_runs(id) on delete restrict,
  deleted_at        timestamptz
);

-- Global (not scoped to live rows): a source record is ingested exactly once, ever.
create unique index if not exists uq_signals_dedupe_key
  on public.signals (dedupe_key);
create index if not exists idx_signals_fund_observed
  on public.signals (fund_id, observed_at desc);
create index if not exists idx_signals_type_urgency
  on public.signals (signal_type, urgency);
create index if not exists idx_signals_person_id on public.signals (person_id);
create index if not exists idx_signals_superseded_by on public.signals (superseded_by);
create index if not exists idx_signals_source_run_id on public.signals (source_run_id);
create index if not exists idx_signals_created_at on public.signals (created_at);
create index if not exists idx_signals_urgent_live
  on public.signals (urgency) where urgency in ('immediate','this_week');

drop trigger if exists trg_signals_updated_at on public.signals;
create trigger trg_signals_updated_at
  before update on public.signals
  for each row execute function public.set_updated_at();

-- ── scoring_runs ────────────────────────────────────────────────────────────
-- entity_id is deliberately polymorphic (no FK): this is an append-only audit
-- log keyed by (entity_type, entity_id).
create table if not exists public.scoring_runs (
  id             uuid primary key default public.uuid_generate_v7(),
  entity_type    text not null check (entity_type in ('fund','signal','person')),
  entity_id      uuid not null,
  model_version  text not null,
  score          numeric,
  reasoning      text,
  inputs         jsonb not null default '{}'::jsonb,
  run_at         timestamptz not null default now(),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  created_by     text not null default 'system',
  source_run_id  uuid references public.source_runs(id) on delete restrict,
  deleted_at     timestamptz
);

create index if not exists idx_scoring_runs_entity
  on public.scoring_runs (entity_type, entity_id, run_at desc);
create index if not exists idx_scoring_runs_source_run_id
  on public.scoring_runs (source_run_id);
create index if not exists idx_scoring_runs_created_at
  on public.scoring_runs (created_at);

drop trigger if exists trg_scoring_runs_updated_at on public.scoring_runs;
create trigger trg_scoring_runs_updated_at
  before update on public.scoring_runs
  for each row execute function public.set_updated_at();
