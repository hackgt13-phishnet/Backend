-- Let room members read their room through Supabase (REST + Realtime).
-- current_demo_profile_id() read demo_identities as the caller, but demo_identities is RLS-locked,
-- so it always returned null and members could read nothing. Run it as the owner instead.

create or replace function current_demo_profile_id() returns uuid
  language sql stable security definer set search_path = public as $$
  select profile_id from demo_identities where user_id = auth.uid()
$$;

-- Membership check that doesn't recurse through room_members' own policy.
create or replace function is_room_member(target_room uuid) returns boolean
  language sql stable security definer set search_path = public as $$
  select exists (select 1 from room_members where room_id = target_room and profile_id = current_demo_profile_id())
$$;

create policy "members can see who is in their room" on room_members for select using (is_room_member(room_id));
create policy "members can see their room's games" on game_sessions for select using (is_room_member(room_id));
