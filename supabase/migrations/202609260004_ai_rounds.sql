-- AI-generated rounds. Answers live in their own table so clients can't read them before the reveal.

create table round_answers (
  round_id uuid primary key references rounds(id) on delete cascade,
  answer jsonb,                      -- null for vote games like Most Likely To
  source_item_ids uuid[] not null
);
-- Backend only: no policies, and not in the Realtime publication.
alter table round_answers enable row level security;

insert into round_answers(round_id, answer, source_item_ids)
select id, answer, source_item_ids from rounds;

alter table rounds
  drop column answer,
  drop column source_item_ids,
  add column moment_id uuid references moments(id);

-- A round can be written ahead of time and wait until the previous one finishes.
-- 'pending' already exists in round_phase for exactly this.
