-- Group memory: embeddings on shared content, and the moments clustered from them.

create extension if not exists vector;

alter table group_context_items
  add column sender_profile_id uuid references profiles(id),
  add column embedding vector(384);

create type moment_kind as enum ('moment', 'inside_joke');

create table moments (
  id uuid primary key,
  label text not null,
  kind moment_kind not null,
  keywords text[] not null default '{}',
  item_ids uuid[] not null,
  participant_profile_ids uuid[] not null,
  first_at timestamptz not null,
  last_at timestamptz not null,
  centroid vector(384) not null,
  created_at timestamptz not null default now()
);

-- Backend only: no policies means clients can't read raw content, embeddings or moments.
alter table profiles enable row level security;
alter table demo_identities enable row level security;
alter table group_context_items enable row level security;
alter table moments enable row level security;

-- Profiles are safe to show to signed-in players (names and avatars only).
create policy "signed-in users can read profiles" on profiles for select to authenticated using (true);
