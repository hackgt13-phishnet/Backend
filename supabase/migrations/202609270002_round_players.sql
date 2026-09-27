-- Who was dealt into each round, so clients can show whose turn it is when options aren't players
-- (AI opinion rounds use free-text options like "agree"/"disagree").
alter table public.rounds
  add column if not exists player_profile_ids uuid[] not null default '{}';
