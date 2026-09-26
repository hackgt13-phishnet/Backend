-- Public Realtime state on top of migrations 001-008.
-- Hidden answers stay in round_answers. reveal_copy moves there too so the
-- published rounds row cannot leak it before reveal.
begin;
set local lock_timeout = '5s';

lock table public.demo_identities, public.rooms, public.room_members,
  public.game_sessions, public.rounds, public.round_responses, public.round_answers
  in share row exclusive mode;

do $$
declare schema_publication boolean := false;
begin
  if not exists (select 1 from pg_roles where rolname = current_user
                 and (rolsuper or rolbypassrls)) then
    raise exception 'Migration owner must bypass RLS for the membership helper';
  end if;
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime'
             and (puballtables or not pubinsert or not pubupdate)) then
    raise exception 'Realtime requires explicit tables with INSERT and UPDATE enabled';
  end if;
  if to_regclass('pg_catalog.pg_publication_namespace') is not null then
    execute 'select exists (select 1 from pg_catalog.pg_publication_namespace n
             join pg_catalog.pg_publication p on p.oid=n.pnpubid
             where p.pubname=''supabase_realtime'')' into schema_publication;
  end if;
  if schema_publication then
    raise exception 'Review schema-wide Realtime publication before this table-scoped migration';
  end if;
end $$;

alter table public.round_answers add column if not exists reveal_copy text;
update public.round_answers a
set reveal_copy = r.reveal_copy
from public.rounds r
where r.id = a.round_id and a.reveal_copy is null;
alter table public.rounds drop column if exists reveal_copy;

create schema if not exists private;
revoke create on schema private from public, anon, authenticated;
grant usage on schema private to authenticated, service_role;

create table if not exists private.round_secrets (
  round_id uuid primary key references public.rounds(id) on delete cascade,
  answer jsonb not null,
  reveal_copy text not null,
  source_item_ids uuid[] not null default '{}',
  eligible_profile_ids uuid[] not null
);
alter table private.round_secrets enable row level security;
revoke all on private.round_secrets from public, anon, authenticated;
grant all on private.round_secrets to service_role;

insert into private.round_secrets(round_id, answer, reveal_copy, source_item_ids, eligible_profile_ids)
select a.round_id,
       coalesce(a.answer, 'null'::jsonb),
       coalesce(a.reveal_copy, ''),
       a.source_item_ids,
       coalesce(array(
         select m.profile_id from public.room_members m
         join public.game_sessions s on s.room_id = m.room_id
         where s.id = r.session_id
         order by m.profile_id
       ), '{}')
from public.round_answers a
join public.rounds r on r.id = a.round_id
on conflict (round_id) do nothing;

alter table public.rooms add column if not exists revision bigint not null default 1;
alter table public.room_members add column if not exists left_at timestamptz;
alter table public.room_members add column if not exists revision bigint not null default 1;
alter table public.game_sessions add column if not exists current_round_ordinal smallint not null default 1;
alter table public.game_sessions add column if not exists revision bigint not null default 1;
alter table public.rounds add column if not exists room_id uuid;
alter table public.rounds add column if not exists media jsonb not null default '{}'::jsonb;
alter table public.rounds add column if not exists required_response_count integer not null default 0;
alter table public.rounds add column if not exists submitted_profile_ids uuid[] not null default '{}';
alter table public.rounds add column if not exists reveal jsonb;
alter table public.rounds add column if not exists revision bigint not null default 1;

update public.rounds r set room_id = s.room_id
from public.game_sessions s
where s.id = r.session_id and r.room_id is null;
update public.rounds r set
  required_response_count = coalesce((
    select count(*) from public.room_members m
    join public.game_sessions s on s.room_id = m.room_id
    where s.id = r.session_id and m.left_at is null
  ), 0),
  submitted_profile_ids = coalesce(array(
    select rr.profile_id from public.round_responses rr
    where rr.round_id = r.id order by rr.profile_id
  ), '{}')
where cardinality(r.submitted_profile_ids) = 0;
update public.game_sessions s set current_round_ordinal = coalesce(
  (select max(r.ordinal) from public.rounds r where r.session_id = s.id and r.phase <> 'pending'), 1
);

alter table public.rounds alter column room_id set not null;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'game_sessions_id_room_unique') then
    alter table public.game_sessions add constraint game_sessions_id_room_unique unique (id, room_id);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'rounds_session_room_fk') then
    alter table public.rounds add constraint rounds_session_room_fk
      foreign key (session_id, room_id) references public.game_sessions(id, room_id) on delete cascade;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'demo_identity_profile_unique') then
    alter table public.demo_identities add constraint demo_identity_profile_unique unique (profile_id);
  end if;
  if not exists (select 1 from pg_indexes where indexname = 'game_sessions_one_active_room') then
    create unique index game_sessions_one_active_room on public.game_sessions(room_id) where status = 'active';
  end if;
end $$;

create or replace function private.bump_revision() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.revision := old.revision + 1;
  return new;
end $$;
revoke all on function private.bump_revision() from public, anon, authenticated;

do $$
begin
  if not exists (select 1 from pg_trigger where tgname = 'rooms_revision') then
    create trigger rooms_revision before update on public.rooms
      for each row execute function private.bump_revision();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'members_revision') then
    create trigger members_revision before update on public.room_members
      for each row execute function private.bump_revision();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'sessions_revision') then
    create trigger sessions_revision before update on public.game_sessions
      for each row execute function private.bump_revision();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'rounds_revision') then
    create trigger rounds_revision before update on public.rounds
      for each row execute function private.bump_revision();
  end if;
end $$;

create or replace function private.is_room_member(target_room uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.demo_identities d
    join public.room_members m on m.profile_id = d.profile_id
    where d.user_id = (select auth.uid()) and m.room_id = target_room and m.left_at is null
  )
$$;
revoke all on function private.is_room_member(uuid) from public, anon, authenticated;
grant execute on function private.is_room_member(uuid) to authenticated;

drop policy if exists "room members can read rooms" on public.rooms;
drop policy if exists "room members can read timeline" on public.timeline_events;
drop policy if exists "room members can read rounds" on public.rounds;
drop policy if exists "participants can see response status" on public.round_responses;
drop policy if exists "members can see who is in their room" on public.room_members;
drop policy if exists "members can see their room's games" on public.game_sessions;
drop policy if exists member_rooms on public.rooms;
drop policy if exists member_roster on public.room_members;
drop policy if exists member_sessions on public.game_sessions;
drop policy if exists member_rounds on public.rounds;
drop policy if exists member_timeline on public.timeline_events;
drop policy if exists demo_profiles on public.profiles;

revoke all on public.rooms, public.room_members, public.game_sessions, public.rounds,
  public.timeline_events, public.round_responses, public.round_answers, public.demo_identities,
  public.group_context_items, public.profiles, public.moments, public.gm_decisions,
  public.player_activity, public.player_interests
  from public, anon, authenticated;
grant select on public.rooms, public.room_members, public.game_sessions, public.rounds,
  public.timeline_events, public.profiles to authenticated;
grant all on public.rooms, public.room_members, public.game_sessions, public.rounds,
  public.timeline_events, public.round_responses, public.round_answers, public.demo_identities,
  public.group_context_items, public.profiles, public.moments, public.gm_decisions,
  public.player_activity, public.player_interests, private.round_secrets
  to service_role;

create policy member_rooms on public.rooms for select to authenticated
  using (private.is_room_member(id));
create policy member_roster on public.room_members for select to authenticated
  using (private.is_room_member(room_id));
create policy member_sessions on public.game_sessions for select to authenticated
  using (private.is_room_member(room_id));
create policy member_rounds on public.rounds for select to authenticated
  using (private.is_room_member(room_id) and phase <> 'pending');
create policy member_timeline on public.timeline_events for select to authenticated
  using (private.is_room_member(room_id));
create policy demo_profiles on public.profiles for select to authenticated using (true);

do $$
declare t text;
begin
  if not exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    create publication supabase_realtime;
  end if;
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime' and puballtables) then
    raise exception 'supabase_realtime must not publish ALL TABLES';
  end if;
  foreach t in array array[
    'round_responses', 'round_answers', 'group_context_items', 'demo_identities',
    'moments', 'gm_decisions', 'player_activity', 'player_interests'
  ] loop
    if exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime'
               and schemaname = 'public' and tablename = t) then
      execute format('alter publication supabase_realtime drop table public.%I', t);
    end if;
  end loop;
  if exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime'
             and schemaname = 'public' and tablename = 'rounds') then
    alter publication supabase_realtime drop table public.rounds;
  end if;
  alter publication supabase_realtime add table public.rounds (
    id, room_id, session_id, ordinal, game_type, phase, prompt, options, media,
    required_response_count, submitted_profile_ids, reveal, revision
  );
  foreach t in array array['rooms', 'room_members', 'game_sessions', 'timeline_events'] loop
    if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime'
                   and schemaname = 'public' and tablename = t) then
      execute format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end $$;
commit;
