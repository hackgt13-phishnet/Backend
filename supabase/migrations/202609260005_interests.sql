-- Interest path: each player's own activity, what Muse learned from it, and rounds built on it.

alter type game_type add value if not exists 'hot_take';
alter type game_type add value if not exists 'this_or_that';

create table player_activity (
  id uuid primary key,
  owner_profile_id uuid not null references profiles(id) on delete cascade,
  kind text not null check (kind in ('post', 'story', 'liked_reel', 'saved', 'follow')),
  -- likes, saves and follows are private: usable as a topic, never quoted to other players
  visibility text not null check (visibility in ('public', 'private')),
  text text not null,
  occurred_at timestamptz not null
);
create index player_activity_owner_idx on player_activity(owner_profile_id);

-- Muse's reading of a player's activity, cached until their activity changes.
create table player_interests (
  profile_id uuid primary key references profiles(id) on delete cascade,
  interests jsonb not null,
  activity_count integer not null,
  computed_at timestamptz not null default now()
);

-- Backend only.
alter table player_activity enable row level security;
alter table player_interests enable row level security;
