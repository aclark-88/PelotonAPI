-- 09 · Business functions, triggers, and RLS
--
-- compute_is_buying_committee  keeps people.is_buying_committee_member in sync
-- validate_fund_strategies     funds.strategies must exist in strategy_types
-- fn_observe_job_change        the job-change transaction (see below)
-- RLS                          enabled everywhere; service_role full access,
--                              anon nothing. Defense in depth, not auth.

-- ── buying committee membership ─────────────────────────────────────────────
-- Buyer roles for Clarion: senior (c_suite / head) people in tech, risk, ops,
-- finance, or executive functions. Adjust here as the ICP sharpens; it is a
-- cached convenience flag, recomputed on every write.
create or replace function public.compute_is_buying_committee()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.is_buying_committee_member :=
    new.current_role_seniority in ('c_suite', 'head')
    and new.current_role_function in ('tech', 'risk', 'ops', 'finance', 'executive');
  return new;
end;
$$;

drop trigger if exists trg_people_buying_committee on public.people;
create trigger trg_people_buying_committee
  before insert or update of current_role_seniority, current_role_function
  on public.people
  for each row execute function public.compute_is_buying_committee();

-- ── strategy vocabulary enforcement ─────────────────────────────────────────
-- A lookup-table FK can't constrain array elements, so a trigger does it.
-- Adding a strategy = INSERT into strategy_types; no migration.
create or replace function public.validate_fund_strategies()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  bad text;
begin
  select s into bad
  from unnest(new.strategies) as s
  where not exists (
    select 1 from public.strategy_types st where st.key = s and st.active
  )
  limit 1;

  if bad is not null then
    raise exception 'unknown or inactive strategy "%" — add it to strategy_types first', bad
      using errcode = '23514';
  end if;
  return new;
end;
$$;

drop trigger if exists trg_funds_validate_strategies on public.funds;
create trigger trg_funds_validate_strategies
  before insert or update of strategies
  on public.funds
  for each row execute function public.validate_fund_strategies();

-- ── the job-change transaction ──────────────────────────────────────────────
-- Emitted signal type. seed.py owns the full vocabulary; this row is created
-- here so the function below can never hit a missing FK.
insert into public.signal_types (key, display_name, default_urgency, default_score_weight, description)
values ('new_role', 'Champion changed role', 'this_week', 8,
        'A tracked person moved to a new fund/role. Emitted by fn_observe_job_change.')
on conflict (key) do nothing;

-- Closes the person''s open employment rows, inserts the new job, updates
-- people.current_* (which re-fires the buying-committee trigger), and emits a
-- new_role signal — atomically. Idempotent: the signal''s source_record_id is
-- deterministic (person:fund:date), so re-observing the same change dedupes.
-- Returns the signal id (existing one if deduped).
create or replace function public.fn_observe_job_change(
  p_person_id     uuid,
  p_new_fund_id   uuid,
  p_new_role      text,
  p_observed_at   timestamptz default now(),
  p_function      public.role_function default 'unknown',
  p_seniority     public.seniority_level default 'unknown',
  p_source        text default 'manual',
  p_source_run_id uuid default null
)
returns uuid
language plpgsql
set search_path = ''
as $$
declare
  v_signal_id   uuid;
  v_record_id   text;
  v_old_fund_id uuid;
  v_old_role    text;
begin
  select current_fund_id, "current_role"
    into v_old_fund_id, v_old_role
  from public.people
  where id = p_person_id and deleted_at is null
  for update;

  if not found then
    raise exception 'person % not found or deleted', p_person_id;
  end if;

  if not exists (select 1 from public.funds where id = p_new_fund_id and deleted_at is null) then
    raise exception 'fund % not found or deleted', p_new_fund_id;
  end if;

  -- close any open employment rows
  update public.employment_history
     set ended_at = p_observed_at::date
   where person_id = p_person_id
     and ended_at is null
     and deleted_at is null;

  -- record the new job
  insert into public.employment_history
    (person_id, fund_id, role, "function", seniority, started_at, source, source_run_id, created_by)
  values
    (p_person_id, p_new_fund_id, p_new_role, p_function, p_seniority,
     p_observed_at::date, p_source, p_source_run_id, 'fn_observe_job_change');

  -- update the person''s current state (fires the buying-committee trigger)
  update public.people
     set current_fund_id        = p_new_fund_id,
         "current_role"         = p_new_role,
         current_role_function  = p_function,
         current_role_seniority = p_seniority,
         role_started_at        = p_observed_at::date,
         source_run_id          = coalesce(p_source_run_id, source_run_id)
   where id = p_person_id;

  -- emit the signal (deduped on source + record id + type)
  v_record_id := p_person_id::text || ':' || p_new_fund_id::text || ':' || (p_observed_at::date)::text;

  insert into public.signals
    (fund_id, person_id, signal_type, source, source_record_id, observed_at,
     urgency, payload, source_run_id, created_by)
  values
    (p_new_fund_id, p_person_id, 'new_role', p_source, v_record_id, p_observed_at,
     coalesce((select st.default_urgency from public.signal_types st where st.key = 'new_role'),
              'this_week'::public.urgency_level),
     jsonb_build_object(
       'person_id',        p_person_id,
       'new_fund_id',      p_new_fund_id,
       'new_role',         p_new_role,
       'previous_fund_id', v_old_fund_id,
       'previous_role',    v_old_role,
       'function',         p_function,
       'seniority',        p_seniority),
     p_source_run_id, 'fn_observe_job_change')
  on conflict (dedupe_key) do nothing
  returning id into v_signal_id;

  if v_signal_id is null then
    select id into v_signal_id
    from public.signals
    where source = p_source
      and source_record_id = v_record_id
      and signal_type = 'new_role';
  end if;

  return v_signal_id;
end;
$$;

-- ── RLS: everything locked; service_role explicit full access ───────────────
-- The Python client uses the service role key (which also has BYPASSRLS);
-- these policies are documented defense in depth. anon/authenticated get no
-- policies and therefore no access.
do $$
declare
  t text;
begin
  foreach t in array array[
    'signal_types', 'strategy_types',
    'source_runs', 'raw_payloads',
    'funds', 'people', 'employment_history',
    'signals', 'scoring_runs',
    'campaigns', 'drafts', 'outreach_attempts', 'replies',
    'hubspot_sync', 'fund_summaries'
  ]
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists service_role_all on public.%I', t);
    execute format(
      'create policy service_role_all on public.%I for all to service_role using (true) with check (true)', t);
  end loop;
end;
$$;
