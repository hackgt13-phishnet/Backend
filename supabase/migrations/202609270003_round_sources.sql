-- Where each round came from, so the AI's work is visible. `about` is safe before the reveal
-- (never names who sent it); the full source note is secret until the reveal.
alter table public.rounds add column if not exists about text;
alter table private.round_secrets add column if not exists source_note text;
