-- Demo profiles are a fixed pool; a phone that closed its tab would otherwise hold its profile forever.
-- last_seen_at lets a new phone take over a profile nobody has used for a while.
alter table public.demo_identities
  add column if not exists last_seen_at timestamptz not null default now();

-- Existing claims haven't been seen since they were made, as far as we know.
update public.demo_identities set last_seen_at = created_at;
