-- Consent: before a game uses anything, each player can see their own material and take items out.
-- Backend only, like the material itself.
create table if not exists material_exclusions (
  profile_id uuid not null references profiles(id) on delete cascade,
  item_id uuid not null,  -- a group_context_items id or a player_activity id
  created_at timestamptz not null default now(),
  primary key (profile_id, item_id)
);
alter table material_exclusions enable row level security;

-- Cached interests are keyed on the exact items they were read from, so taking one item out and
-- putting another back can never leave interests built from something a player removed.
alter table player_interests add column if not exists activity_key text;

-- One closing line when a game ends.
alter type timeline_event_type add value if not exists 'game_recap';
