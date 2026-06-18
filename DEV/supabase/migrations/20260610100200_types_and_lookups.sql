-- 02 · Enum types and lookup tables
--
-- Enums are reserved for closed vocabularies that change rarely (seniority,
-- pipeline statuses). Anything that grows with new data sources — signal
-- types, strategies — is a lookup TABLE so additions are INSERTs, never
-- migrations. Extending an enum later is still additive:
--   alter type public.<enum> add value '<new>';

do $$ begin
  create type public.urgency_level as enum
    ('immediate','this_week','this_month','archive');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.seniority_level as enum
    ('c_suite','head','vp','ic','unknown');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.role_function as enum
    ('tech','risk','ops','finance','trading','investment','executive','unknown');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.outreach_status as enum
    ('queued','sent','delivered','opened','replied','bounced','failed','unsubscribed');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.reply_sentiment as enum
    ('positive','neutral','negative','autoresponder','ooo');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.reply_intent as enum
    ('meeting_request','objection','unsubscribe','referral','nurture');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.sync_status as enum
    ('pending','synced','failed');
exception when duplicate_object then null; end $$;

do $$ begin
  create type public.run_status as enum
    ('running','success','failed','partial');
exception when duplicate_object then null; end $$;

-- ── signal_types ────────────────────────────────────────────────────────────
-- The vocabulary of observable events. A new signal type is one INSERT
-- (see gtm/db/seed.py); no DDL ever required.
create table if not exists public.signal_types (
  key                   text primary key,
  display_name          text not null,
  default_urgency       public.urgency_level not null default 'this_month',
  default_score_weight  numeric not null default 1.0,
  active                boolean not null default true,
  description           text,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  created_by            text not null default 'system'
);

-- ── strategy_types ──────────────────────────────────────────────────────────
-- Allowed values for funds.strategies (validated by trigger, migration 09).
create table if not exists public.strategy_types (
  key           text primary key,
  display_name  text not null,
  active        boolean not null default true,
  description   text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  created_by    text not null default 'system'
);

-- Initial strategy vocabulary (ICP core + adjacent). Add rows, never columns.
insert into public.strategy_types (key, display_name) values
  ('macro',             'Global Macro'),
  ('credit',            'Credit'),
  ('fixed_income',      'Fixed Income'),
  ('relative_value',    'Relative Value'),
  ('multi_strategy',    'Multi-Strategy'),
  ('structured_credit', 'Structured Credit'),
  ('volatility_arb',    'Volatility Arbitrage'),
  ('convertible_arb',   'Convertible Arbitrage'),
  ('equity_long_short', 'Equity Long/Short'),
  ('event_driven',      'Event Driven'),
  ('quant',             'Quantitative'),
  ('commodities',       'Commodities'),
  ('crypto',            'Digital Assets'),
  ('other',             'Other')
on conflict (key) do nothing;

drop trigger if exists trg_signal_types_updated_at on public.signal_types;
create trigger trg_signal_types_updated_at
  before update on public.signal_types
  for each row execute function public.set_updated_at();

drop trigger if exists trg_strategy_types_updated_at on public.strategy_types;
create trigger trg_strategy_types_updated_at
  before update on public.strategy_types
  for each row execute function public.set_updated_at();
