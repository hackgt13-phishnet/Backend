-- Real posts are photos of people at places. A post can carry its photo, where it was taken, and who
-- was tagged; Muse's structured read of the photo is cached with it. Backend only, like the rest.
alter table player_activity
  add column if not exists media_url text,
  add column if not exists media_credit text,
  add column if not exists media_read jsonb,
  add column if not exists location text,
  add column if not exists tagged_profile_ids uuid[] not null default '{}';

-- A photo that shows a person gives away "who sent this?", so rounds need to know.
alter table group_context_items add column if not exists shows_person boolean not null default false;
