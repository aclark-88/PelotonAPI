-- 01 · Extensions and shared helpers
--
-- citext   case-insensitive text (domains, emails)
-- pg_trgm  trigram fuzzy matching on fund / person names
-- vector   pgvector embeddings (drafts, replies, fund summaries)

create extension if not exists citext with schema extensions;
create extension if not exists pg_trgm with schema extensions;
create extension if not exists vector with schema extensions;

-- Time-ordered UUIDs. Postgres 17 has no native uuidv7(); this is the standard
-- polyfill: millisecond unix timestamp in the top 48 bits, version (7) and
-- variant bits set, remaining bits random. Swap for native uuidv7() on PG18+.
create or replace function public.uuid_generate_v7()
returns uuid
language sql
volatile
parallel safe
set search_path = ''
as $$
  select encode(
    set_bit(
      set_bit(
        overlay(uuid_send(gen_random_uuid())
                placing substring(int8send((extract(epoch from clock_timestamp()) * 1000)::bigint) from 3)
                from 1 for 6),
        52, 1),
      53, 1),
    'hex')::uuid;
$$;

-- Shared updated_at maintenance. Attached per-table in the migration that
-- creates the table.
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;
