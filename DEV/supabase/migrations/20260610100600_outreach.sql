-- 06 · Outreach state: campaigns, drafts, outreach_attempts, replies
--
-- drafts and outreach_attempts reference each other (draft → the attempt that
-- sent it; attempt → the draft it sent). Both columns are nullable and the
-- second FK is added after both tables exist.

create table if not exists public.campaigns (
  id                   uuid primary key default public.uuid_generate_v7(),
  name                 text not null,
  signal_type_key      text references public.signal_types(key) on delete restrict,
  channel              text not null default 'email' check (channel in ('email','linkedin','multi')),
  apollo_sequence_id   text,
  heyreach_campaign_id text,
  active               boolean not null default true,
  metadata             jsonb not null default '{}'::jsonb,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  created_by           text not null default 'system',
  source_run_id        uuid references public.source_runs(id) on delete restrict,
  deleted_at           timestamptz
);

create unique index if not exists uq_campaigns_name
  on public.campaigns (name) where deleted_at is null;
create index if not exists idx_campaigns_signal_type_key on public.campaigns (signal_type_key);
create index if not exists idx_campaigns_source_run_id on public.campaigns (source_run_id);
create index if not exists idx_campaigns_created_at on public.campaigns (created_at);

drop trigger if exists trg_campaigns_updated_at on public.campaigns;
create trigger trg_campaigns_updated_at
  before update on public.campaigns
  for each row execute function public.set_updated_at();

-- ── drafts ──────────────────────────────────────────────────────────────────
create table if not exists public.drafts (
  id               uuid primary key default public.uuid_generate_v7(),
  person_id        uuid not null references public.people(id) on delete restrict,
  signal_id        uuid references public.signals(id) on delete restrict,
  campaign_id      uuid references public.campaigns(id) on delete restrict,
  channel          text not null default 'email' check (channel in ('email','linkedin')),
  variant_label    text,                          -- A, B, C
  subject          text,
  body             text not null,
  model            text,                          -- generating model id
  prompt_version   text,
  approved_by      text,                          -- human reviewer; null = not approved
  approved_at      timestamptz,
  sent_attempt_id  uuid,                          -- FK added below (circular)
  embedding        extensions.vector(1536),
  metadata         jsonb not null default '{}'::jsonb,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  created_by       text not null default 'system',
  source_run_id    uuid references public.source_runs(id) on delete restrict,
  deleted_at       timestamptz
);

create index if not exists idx_drafts_person_id on public.drafts (person_id);
create index if not exists idx_drafts_signal_id on public.drafts (signal_id);
create index if not exists idx_drafts_campaign_id on public.drafts (campaign_id);
create index if not exists idx_drafts_sent_attempt_id on public.drafts (sent_attempt_id);
create index if not exists idx_drafts_source_run_id on public.drafts (source_run_id);
create index if not exists idx_drafts_created_at on public.drafts (created_at);

drop trigger if exists trg_drafts_updated_at on public.drafts;
create trigger trg_drafts_updated_at
  before update on public.drafts
  for each row execute function public.set_updated_at();

-- ── outreach_attempts ───────────────────────────────────────────────────────
create table if not exists public.outreach_attempts (
  id           uuid primary key default public.uuid_generate_v7(),
  person_id    uuid not null references public.people(id) on delete restrict,
  campaign_id  uuid not null references public.campaigns(id) on delete restrict,
  signal_id    uuid references public.signals(id) on delete restrict,  -- the trigger signal
  channel      text not null default 'email' check (channel in ('email','linkedin')),
  step_number  integer not null default 1,
  sent_at      timestamptz,
  status       public.outreach_status not null default 'queued',
  external_id  text,                              -- Apollo / HeyReach message id
  draft_id     uuid references public.drafts(id) on delete restrict,
  metadata     jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  created_by   text not null default 'system',
  source_run_id uuid references public.source_runs(id) on delete restrict,
  deleted_at   timestamptz
);

-- Hard duplicate-send guard: not scoped to deleted_at on purpose.
create unique index if not exists uq_outreach_attempts_person_campaign_step
  on public.outreach_attempts (person_id, campaign_id, step_number);
create index if not exists idx_outreach_attempts_person_sent
  on public.outreach_attempts (person_id, sent_at desc);
create index if not exists idx_outreach_attempts_campaign_id on public.outreach_attempts (campaign_id);
create index if not exists idx_outreach_attempts_signal_id on public.outreach_attempts (signal_id);
create index if not exists idx_outreach_attempts_draft_id on public.outreach_attempts (draft_id);
create index if not exists idx_outreach_attempts_source_run_id on public.outreach_attempts (source_run_id);
create index if not exists idx_outreach_attempts_created_at on public.outreach_attempts (created_at);

drop trigger if exists trg_outreach_attempts_updated_at on public.outreach_attempts;
create trigger trg_outreach_attempts_updated_at
  before update on public.outreach_attempts
  for each row execute function public.set_updated_at();

-- Close the circular reference now that both tables exist.
do $$ begin
  alter table public.drafts
    add constraint fk_drafts_sent_attempt
    foreign key (sent_attempt_id) references public.outreach_attempts(id) on delete restrict;
exception when duplicate_object then null; end $$;

-- ── replies ─────────────────────────────────────────────────────────────────
create table if not exists public.replies (
  id                   uuid primary key default public.uuid_generate_v7(),
  outreach_attempt_id  uuid not null references public.outreach_attempts(id) on delete restrict,
  received_at          timestamptz not null default now(),
  body                 text,
  sentiment            public.reply_sentiment,
  intent               public.reply_intent,
  embedding            extensions.vector(1536),
  metadata             jsonb not null default '{}'::jsonb,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  created_by           text not null default 'system',
  source_run_id        uuid references public.source_runs(id) on delete restrict,
  deleted_at           timestamptz
);

create index if not exists idx_replies_outreach_attempt_id on public.replies (outreach_attempt_id);
create index if not exists idx_replies_source_run_id on public.replies (source_run_id);
create index if not exists idx_replies_created_at on public.replies (created_at);

drop trigger if exists trg_replies_updated_at on public.replies;
create trigger trg_replies_updated_at
  before update on public.replies
  for each row execute function public.set_updated_at();
