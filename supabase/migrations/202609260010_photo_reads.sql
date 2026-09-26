-- What Muse saw in each photo (read once, offline) and who took it. Backend only, like the rest of the table.

alter table group_context_items
  add column if not exists media_description text,
  add column if not exists media_credit text;
