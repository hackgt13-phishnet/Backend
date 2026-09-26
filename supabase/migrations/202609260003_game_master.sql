-- Game master: round timing for the conductor, and a log of every decision it makes.

alter type timeline_event_type add value if not exists 'host_line';

alter table rounds
  add column opened_at timestamptz,
  add column revealed_at timestamptz,
  add column story_holder_profile_id uuid references profiles(id),
  add column nudges smallint not null default 0;

create table gm_decisions (
  id bigint generated always as identity primary key,
  room_id uuid not null references rooms(id) on delete cascade,
  round_id uuid references rounds(id) on delete cascade,
  action text not null check (action in ('wait', 'reveal', 'nudge', 'next_round')),
  reason text not null,
  p_silence real,
  model_source text not null,
  target_profile_id uuid references profiles(id),
  features jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create index gm_decisions_room_created_idx on gm_decisions(room_id, created_at);

-- Backend only. The debug view reads it through the API.
alter table gm_decisions enable row level security;
