-- 10 · fn_observe_job_change: full idempotency
--
-- The original function deduped the emitted signal but still closed and
-- re-inserted an employment_history row when the observation matched the
-- person's current state — re-observing the same job change churned history
-- with duplicate rows. Now: if the person is already at (fund, role), the
-- employment timeline is untouched (only function/seniority metadata is
-- refreshed) and the deduped signal id is returned.

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
  v_changed     boolean;
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

  v_changed := (v_old_fund_id is distinct from p_new_fund_id)
            or (v_old_role is distinct from p_new_role);

  if v_changed then
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

    -- update the person's current state (fires the buying-committee trigger)
    update public.people
       set current_fund_id        = p_new_fund_id,
           "current_role"         = p_new_role,
           current_role_function  = p_function,
           current_role_seniority = p_seniority,
           role_started_at        = p_observed_at::date,
           source_run_id          = coalesce(p_source_run_id, source_run_id)
     where id = p_person_id;
  else
    -- re-observation of the current job: leave the timeline alone, but pick
    -- up better function/seniority classification if the source provides it
    update public.people
       set current_role_function  = p_function,
           current_role_seniority = p_seniority
     where id = p_person_id
       and p_function <> 'unknown'
       and (current_role_function is distinct from p_function
            or current_role_seniority is distinct from p_seniority);
  end if;

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
