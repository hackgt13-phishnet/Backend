-- For Who Sent This?, the story holder is the sender, i.e. the answer. Move it out of rounds
-- (readable by members and broadcast over Realtime) into round_answers (backend only).

alter table round_answers add column story_holder_profile_id uuid references profiles(id);
update round_answers a set story_holder_profile_id = r.story_holder_profile_id from rounds r where r.id = a.round_id;
alter table rounds drop column story_holder_profile_id;
