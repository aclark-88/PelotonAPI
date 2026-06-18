-- 08 · Vector and fuzzy search
--
-- fund_summaries  one embedding-bearing summary per fund (regenerable), for
--                 semantic fund search.
-- HNSW indexes    on all three vector columns (cosine distance).
-- pg_trgm GIN     on fund / person names for fuzzy entity resolution.

create table if not exists public.fund_summaries (
  id               uuid primary key default public.uuid_generate_v7(),
  fund_id          uuid not null references public.funds(id) on delete restrict,
  summary_text     text not null,
  embedding        extensions.vector(1536),
  embedding_model  text,
  generated_at     timestamptz not null default now(),
  metadata         jsonb not null default '{}'::jsonb,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  created_by       text not null default 'system',
  source_run_id    uuid references public.source_runs(id) on delete restrict,
  deleted_at       timestamptz
);

create index if not exists idx_fund_summaries_fund_id on public.fund_summaries (fund_id);
create index if not exists idx_fund_summaries_source_run_id on public.fund_summaries (source_run_id);
create index if not exists idx_fund_summaries_created_at on public.fund_summaries (created_at);

drop trigger if exists trg_fund_summaries_updated_at on public.fund_summaries;
create trigger trg_fund_summaries_updated_at
  before update on public.fund_summaries
  for each row execute function public.set_updated_at();

-- ── HNSW vector indexes (cosine) ────────────────────────────────────────────
create index if not exists hnsw_drafts_embedding
  on public.drafts using hnsw (embedding extensions.vector_cosine_ops);
create index if not exists hnsw_replies_embedding
  on public.replies using hnsw (embedding extensions.vector_cosine_ops);
create index if not exists hnsw_fund_summaries_embedding
  on public.fund_summaries using hnsw (embedding extensions.vector_cosine_ops);

-- ── trigram fuzzy-match indexes ─────────────────────────────────────────────
create index if not exists trgm_funds_legal_name
  on public.funds using gin (legal_name extensions.gin_trgm_ops);
create index if not exists trgm_funds_common_name
  on public.funds using gin (common_name extensions.gin_trgm_ops);
create index if not exists trgm_people_full_name
  on public.people using gin (full_name extensions.gin_trgm_ops);

-- ── semantic search entrypoint ──────────────────────────────────────────────
-- Cosine similarity over live fund summaries. Callable via PostgREST rpc().
create or replace function public.match_fund_summaries(
  query_embedding extensions.vector(1536),
  match_count integer default 10
)
returns table (
  fund_summary_id uuid,
  fund_id uuid,
  summary_text text,
  similarity double precision
)
language sql
stable
set search_path = ''
as $$
  select
    fs.id,
    fs.fund_id,
    fs.summary_text,
    1 - (fs.embedding operator(extensions.<=>) query_embedding) as similarity
  from public.fund_summaries fs
  where fs.embedding is not null
    and fs.deleted_at is null
  order by fs.embedding operator(extensions.<=>) query_embedding
  limit match_count;
$$;
