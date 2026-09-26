-- When the game master last nudged someone, so it gives them time to answer before moving on.

alter table rounds add column last_nudge_at timestamptz;
