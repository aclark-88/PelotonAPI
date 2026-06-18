-- 04 · Core entities: funds, people, employment_history
--
-- Identity uniqueness (crd / lei / domain / linkedin / apollo_id) is enforced
-- with partial unique indexes scoped to live rows (deleted_at is null), so a
-- soft-deleted record never blocks re-ingestion.

-- ── funds ───────────────────────────────────────────────────────────────────
create table if not exists public.funds (
  id                   uuid primary key default public.uuid_generate_v7(),

  -- identity
  legal_name           text not null,
  common_name          text,
  crd                  text,
  lei                  text,
  cik                  text,
  primary_domain       extensions.citext,

  -- classification
  aum_usd_millions     numeric,
  aum_as_of            date,
  aum_band             text generated always as (
                         case
                           when aum_usd_millions is null then 'unknown'
                           when aum_usd_millions < 300   then 'under_300'
                           when aum_usd_millions < 1000  then '300_to_1b'
                           when aum_usd_millions <= 5000 then '1b_to_5b'
                           else 'over_5b'
                         end
                       ) stored,
  strategies           text[] not null default '{}',   -- validated against strategy_types (migration 09)
  is_emerging_manager  boolean,
  parent_fund_id       uuid references public.funds(id) on delete set null,  -- spinout lineage

  -- operational
  headquarters_city    text,
  headquarters_country text,
  inception_date       date,
  prime_brokers        text[] not null default '{}',
  administrator        text,
  custodians           text[] not null default '{}',
  known_incumbent_pms  text[] not null default '{}',

  -- scoring cache (history lives in scoring_runs)
  fit_score            integer check (fit_score between 0 and 100),
  fit_score_updated_at timestamptz,
  tier                 smallint check (tier between 1 and 4),

  notes                text,
  metadata             jsonb not null default '{}'::jsonb,

  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  created_by           text not null default 'system',
  source_run_id        uuid references public.source_runs(id) on delete restrict,
  deleted_at           timestamptz
);

create unique index if not exists uq_funds_crd
  on public.funds (crd) where crd is not null and deleted_at is null;
create unique index if not exists uq_funds_lei
  on public.funds (lei) where lei is not null and deleted_at is null;
create unique index if not exists uq_funds_primary_domain
  on public.funds (primary_domain) where primary_domain is not null and deleted_at is null;
create index if not exists idx_funds_cik on public.funds (cik);
create index if not exists idx_funds_parent_fund_id on public.funds (parent_fund_id);
create index if not exists idx_funds_source_run_id on public.funds (source_run_id);
create index if not exists idx_funds_created_at on public.funds (created_at);
create index if not exists idx_funds_tier_live
  on public.funds (tier) where deleted_at is null;

drop trigger if exists trg_funds_updated_at on public.funds;
create trigger trg_funds_updated_at
  before update on public.funds
  for each row execute function public.set_updated_at();

-- ── people ──────────────────────────────────────────────────────────────────
-- "current_role" is quoted everywhere in SQL: CURRENT_ROLE is a reserved word.
create table if not exists public.people (
  id                          uuid primary key default public.uuid_generate_v7(),
  full_name                   text not null,
  email                       extensions.citext,
  linkedin_url                text,
  apollo_id                   text,
  current_fund_id             uuid references public.funds(id) on delete restrict,
  "current_role"              text,
  current_role_seniority      public.seniority_level not null default 'unknown',
  current_role_function       public.role_function not null default 'unknown',
  role_started_at             date,
  is_buying_committee_member  boolean not null default false,  -- trigger-maintained (migration 09)
  metadata                    jsonb not null default '{}'::jsonb,
  created_at                  timestamptz not null default now(),
  updated_at                  timestamptz not null default now(),
  created_by                  text not null default 'system',
  source_run_id               uuid references public.source_runs(id) on delete restrict,
  deleted_at                  timestamptz
);

create unique index if not exists uq_people_linkedin_url
  on public.people (linkedin_url) where linkedin_url is not null and deleted_at is null;
create unique index if not exists uq_people_apollo_id
  on public.people (apollo_id) where apollo_id is not null and deleted_at is null;
create index if not exists idx_people_email
  on public.people (email) where email is not null;
create index if not exists idx_people_current_fund_id on public.people (current_fund_id);
create index if not exists idx_people_source_run_id on public.people (source_run_id);
create index if not exists idx_people_created_at on public.people (created_at);
create index if not exists idx_people_buying_committee_live
  on public.people (current_fund_id) where is_buying_committee_member and deleted_at is null;

drop trigger if exists trg_people_updated_at on public.people;
create trigger trg_people_updated_at
  before update on public.people
  for each row execute function public.set_updated_at();

-- ── employment_history ──────────────────────────────────────────────────────
-- Append-only job history. Every observed job is a row; rows are closed
-- (ended_at set), never overwritten. Powers "champion moved" alerts.
create table if not exists public.employment_history (
  id             uuid primary key default public.uuid_generate_v7(),
  person_id      uuid not null references public.people(id) on delete restrict,
  fund_id        uuid not null references public.funds(id) on delete restrict,
  role           text,
  "function"     public.role_function not null default 'unknown',
  seniority      public.seniority_level not null default 'unknown',
  started_at     date,
  ended_at       date,                              -- null = current
  source         text,
  metadata       jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  created_by     text not null default 'system',
  source_run_id  uuid references public.source_runs(id) on delete restrict,
  deleted_at     timestamptz
);

create index if not exists idx_employment_history_person_started
  on public.employment_history (person_id, started_at desc);
create index if not exists idx_employment_history_fund_id
  on public.employment_history (fund_id);
create index if not exists idx_employment_history_source_run_id
  on public.employment_history (source_run_id);
create index if not exists idx_employment_history_created_at
  on public.employment_history (created_at);

drop trigger if exists trg_employment_history_updated_at on public.employment_history;
create trigger trg_employment_history_updated_at
  before update on public.employment_history
  for each row execute function public.set_updated_at();
