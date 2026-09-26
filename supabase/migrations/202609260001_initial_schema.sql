create extension if not exists pgcrypto;

create type room_role as enum ('host', 'member');
create type round_phase as enum ('pending', 'answering', 'revealed', 'complete');
create type game_type as enum ('who_sent_this', 'most_likely_to');
create type timeline_event_type as enum ('message', 'game_started', 'game_prompt', 'submission_status', 'game_reveal');

create table profiles (
  id uuid primary key default gen_random_uuid(),
  display_name text not null unique,
  avatar_url text,
  created_at timestamptz not null default now()
);

create table demo_identities (
  user_id uuid primary key references auth.users(id) on delete cascade,
  profile_id uuid not null references profiles(id),
  created_at timestamptz not null default now()
);

create table rooms (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 1 and 80),
  join_code text not null unique check (char_length(join_code) between 6 and 8),
  host_profile_id uuid not null references profiles(id),
  created_at timestamptz not null default now()
);

create table room_members (
  room_id uuid not null references rooms(id) on delete cascade,
  profile_id uuid not null references profiles(id),
  role room_role not null default 'member',
  joined_at timestamptz not null default now(),
  primary key (room_id, profile_id)
);

create table game_sessions (
  id uuid primary key default gen_random_uuid(),
  room_id uuid not null references rooms(id) on delete cascade,
  vibe text not null check (vibe = 'chaos'),
  status text not null default 'active' check (status in ('active', 'complete')),
  created_at timestamptz not null default now()
);

create table group_context_items (
  id uuid primary key default gen_random_uuid(),
  content_type text not null check (content_type in ('message', 'photo', 'reel', 'post')),
  body text,
  media_url text,
  participant_profile_ids uuid[] not null default '{}',
  occurred_at timestamptz not null,
  tags text[] not null default '{}',
  safe_for_demo boolean not null default true
);

create table rounds (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references game_sessions(id) on delete cascade,
  ordinal smallint not null check (ordinal between 1 and 3),
  game_type game_type not null,
  phase round_phase not null default 'pending',
  prompt text not null,
  options jsonb not null,
  answer jsonb not null,
  source_item_ids uuid[] not null,
  reveal_copy text not null,
  unique (session_id, ordinal)
);

create table round_responses (
  round_id uuid not null references rounds(id) on delete cascade,
  profile_id uuid not null references profiles(id),
  value jsonb not null,
  submitted_at timestamptz not null default now(),
  primary key (round_id, profile_id)
);

create table timeline_events (
  id uuid primary key default gen_random_uuid(),
  room_id uuid not null references rooms(id) on delete cascade,
  event_type timeline_event_type not null,
  actor_profile_id uuid references profiles(id),
  payload jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create index timeline_events_room_created_idx on timeline_events(room_id, created_at);
create index room_members_profile_idx on room_members(profile_id);

alter table rooms enable row level security;
alter table room_members enable row level security;
alter table game_sessions enable row level security;
alter table rounds enable row level security;
alter table round_responses enable row level security;
alter table timeline_events enable row level security;

create function current_demo_profile_id() returns uuid language sql stable as $$
  select profile_id from demo_identities where user_id = auth.uid()
$$;

create policy "room members can read rooms" on rooms for select using (
  exists (select 1 from room_members where room_id = rooms.id and profile_id = current_demo_profile_id())
);
create policy "room members can read timeline" on timeline_events for select using (
  exists (select 1 from room_members where room_id = timeline_events.room_id and profile_id = current_demo_profile_id())
);
create policy "room members can read rounds" on rounds for select using (
  exists (select 1 from game_sessions join room_members on room_members.room_id = game_sessions.room_id where game_sessions.id = rounds.session_id and room_members.profile_id = current_demo_profile_id())
);
create policy "participants can see response status" on round_responses for select using (
  profile_id = current_demo_profile_id()
);

alter publication supabase_realtime add table rooms, room_members, rounds, round_responses, timeline_events;
