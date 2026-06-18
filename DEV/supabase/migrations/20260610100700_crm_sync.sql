-- 07 · CRM sync state
--
-- hubspot_sync maps local entities to HubSpot objects and tracks sync state.
-- Supabase stays the source of truth; this table is the reconciliation ledger.
-- local_id is polymorphic on entity_type (fund → funds.id, person → people.id),
-- so no FK — the sync worker validates existence.

create table if not exists public.hubspot_sync (
  id                   uuid primary key default public.uuid_generate_v7(),
  entity_type          text not null check (entity_type in ('fund','person')),
  local_id             uuid not null,
  hubspot_object_type  text not null check (hubspot_object_type in ('company','contact','deal')),
  hubspot_id           text,
  last_synced_at       timestamptz,
  sync_status          public.sync_status not null default 'pending',
  sync_error           text,
  metadata             jsonb not null default '{}'::jsonb,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  created_by           text not null default 'system',
  source_run_id        uuid references public.source_runs(id) on delete restrict,
  deleted_at           timestamptz
);

create unique index if not exists uq_hubspot_sync_entity_object
  on public.hubspot_sync (entity_type, local_id, hubspot_object_type)
  where deleted_at is null;
create index if not exists idx_hubspot_sync_hubspot_id on public.hubspot_sync (hubspot_id);
create index if not exists idx_hubspot_sync_pending
  on public.hubspot_sync (sync_status) where sync_status in ('pending','failed');
create index if not exists idx_hubspot_sync_source_run_id on public.hubspot_sync (source_run_id);
create index if not exists idx_hubspot_sync_created_at on public.hubspot_sync (created_at);

drop trigger if exists trg_hubspot_sync_updated_at on public.hubspot_sync;
create trigger trg_hubspot_sync_updated_at
  before update on public.hubspot_sync
  for each row execute function public.set_updated_at();
