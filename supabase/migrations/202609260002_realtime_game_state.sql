-- Apply after 202609260001. Requires the trusted Supabase migration role.
-- Conflicting existing identity bindings/active sessions fail rather than delete data.
begin;
set local lock_timeout = '5s';

-- Fail before changing shared schema when assumptions from 001 do not hold.
-- These checks never repair/delete teammate data automatically.
lock table public.demo_identities, public.rooms, public.room_members,
  public.game_sessions, public.rounds, public.round_responses in share row exclusive mode;
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
  -- Schema publications (PG15+) can silently include newly created private tables.
  if to_regclass('pg_catalog.pg_publication_namespace') is not null then
    execute 'select exists (select 1 from pg_catalog.pg_publication_namespace n
             join pg_catalog.pg_publication p on p.oid=n.pnpubid
             where p.pubname=''supabase_realtime'')' into schema_publication;
  end if;
  if schema_publication then
    raise exception 'Review schema-wide Realtime publication before this table-scoped migration';
  end if;
  if exists (select 1 from public.demo_identities group by profile_id having count(*) > 1) then
    raise exception 'Multiple auth users claim a demo profile; resolve bindings explicitly first';
  end if;
  if exists (select 1 from public.game_sessions where status='active'
             group by room_id having count(*) > 1) then
    raise exception 'Multiple active sessions in a room; review existing sessions first';
  end if;
  if exists (
    select 1 from public.round_responses rr join public.rounds r on r.id=rr.round_id
    join public.game_sessions s on s.id=r.session_id
    where not exists (select 1 from public.room_members m
                      where m.room_id=s.room_id and m.profile_id=rr.profile_id)
  ) then
    raise exception 'Historical respondent is no longer a room member; reconstruct eligibility first';
  end if;
  if exists (select 1 from public.rounds where jsonb_typeof(options) <> 'array') then
    raise exception 'Legacy round options must be converted to profile_id/label objects first';
  end if;
  if exists (
    select 1 from public.rounds r cross join lateral jsonb_array_elements(r.options) o
    where jsonb_typeof(o) is distinct from 'object'
       or jsonb_typeof(o->'label') is distinct from 'string'
       or jsonb_typeof(o->'profile_id') is distinct from 'string'
       or coalesce(o->>'profile_id','') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  ) then
    raise exception 'Legacy round options are incompatible with the public API; map them explicitly first';
  end if;
  if exists (
    select 1 from public.game_sessions s where s.status='active' and
      (select count(*) from public.rounds r where r.session_id=s.id
       and r.phase in ('answering','revealed')) <> 1
  ) then
    raise exception 'Active session must have exactly one answering/revealed round; review legacy state first';
  end if;
end $$;

create schema if not exists private;
-- Preserve USAGE needed by unrelated existing private-schema policy helpers.
-- Table/function grants below protect this application's objects explicitly.
revoke create on schema private from public, anon, authenticated;
grant usage on schema private to authenticated, service_role;

create table private.round_secrets (
  round_id uuid primary key references public.rounds(id) on delete cascade,
  answer jsonb not null,
  reveal_copy text not null,
  source_item_ids uuid[] not null default '{}',
  eligible_profile_ids uuid[] not null
);
alter table private.round_secrets enable row level security;
revoke all on private.round_secrets from public, anon, authenticated;
grant all on private.round_secrets to service_role;

-- Copy and verify before removing the sensitive public columns.
insert into private.round_secrets(round_id, answer, reveal_copy, source_item_ids, eligible_profile_ids)
select r.id, r.answer, r.reveal_copy, r.source_item_ids,
       array(select m.profile_id from public.room_members m
             join public.game_sessions s on s.room_id = m.room_id
             where s.id = r.session_id order by m.profile_id)
from public.rounds r;

do $$
begin
  if exists (
    select 1 from public.rounds r left join private.round_secrets s on s.round_id = r.id
    where s.round_id is null or s.answer is distinct from r.answer
       or s.reveal_copy is distinct from r.reveal_copy
       or s.source_item_ids is distinct from r.source_item_ids
  ) then
    raise exception 'Round secret copy verification failed';
  end if;
end $$;

alter table public.rounds drop column answer, drop column reveal_copy, drop column source_item_ids;
alter table public.rooms add column revision bigint not null default 1;
alter table public.room_members add column left_at timestamptz,
  add column revision bigint not null default 1;
alter table public.game_sessions
  add column current_round_ordinal smallint not null default 1
    check (current_round_ordinal between 1 and 3),
  add column revision bigint not null default 1,
  add constraint game_sessions_id_room_unique unique (id, room_id);
create unique index game_sessions_one_active_room on public.game_sessions(room_id)
  where status = 'active';

alter table public.rounds
  add column room_id uuid,
  add column media jsonb not null default '{}'::jsonb,
  add column required_response_count integer not null default 0,
  add column submitted_profile_ids uuid[] not null default '{}',
  add column reveal jsonb,
  add column revision bigint not null default 1;
update public.rounds r set room_id = s.room_id
from public.game_sessions s where s.id = r.session_id;
update public.rounds r set
  required_response_count = cardinality(s.eligible_profile_ids),
  submitted_profile_ids = array(select rr.profile_id from public.round_responses rr
                                where rr.round_id = r.id order by rr.profile_id),
  reveal = case when r.phase in ('revealed', 'complete')
    then jsonb_build_object('answer', s.answer, 'message', s.reveal_copy, 'results', '[]'::jsonb)
    else null end
from private.round_secrets s where s.round_id = r.id;
update public.game_sessions s set current_round_ordinal = coalesce(
  (select max(r.ordinal) from public.rounds r where r.session_id = s.id and r.phase <> 'pending'), 1
);
alter table public.rounds alter column room_id set not null,
  add constraint rounds_session_room_fk foreign key (session_id, room_id)
    references public.game_sessions(id, room_id) on delete cascade,
  add constraint rounds_media_object check (jsonb_typeof(media) = 'object'),
  add constraint rounds_response_count check (
    required_response_count >= 0 and cardinality(submitted_profile_ids) <= required_response_count
  ),
  add constraint rounds_reveal_phase check (
    (phase in ('pending', 'answering') and reveal is null)
    or (phase in ('revealed', 'complete') and reveal is not null
        and jsonb_typeof(reveal) = 'object')
  );
create index rounds_room_idx on public.rounds(room_id);
create index game_sessions_room_idx on public.game_sessions(room_id, created_at);
alter table public.demo_identities add constraint demo_identity_profile_unique unique(profile_id);

create function private.bump_revision() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.revision := old.revision + 1;
  return new;
end $$;
revoke all on function private.bump_revision() from public, anon, authenticated;
create trigger rooms_revision before update on public.rooms
  for each row execute function private.bump_revision();
create trigger members_revision before update on public.room_members
  for each row execute function private.bump_revision();
create trigger sessions_revision before update on public.game_sessions
  for each row execute function private.bump_revision();
create trigger rounds_revision before update on public.rounds
  for each row execute function private.bump_revision();

-- This function is owned by the migration role (postgres/BYPASSRLS), preventing
-- recursion when a room_members SELECT policy evaluates membership.
create function private.is_room_member(target_room uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.demo_identities d
    join public.room_members m on m.profile_id = d.profile_id
    where d.user_id = (select auth.uid()) and m.room_id = target_room and m.left_at is null
  )
$$;
revoke all on function private.is_room_member(uuid) from public, anon, authenticated;
grant execute on function private.is_room_member(uuid) to authenticated;

drop policy "room members can read rooms" on public.rooms;
drop policy "room members can read timeline" on public.timeline_events;
drop policy "room members can read rounds" on public.rounds;
drop policy "participants can see response status" on public.round_responses;
revoke all on function public.current_demo_profile_id() from public, anon, authenticated;

alter table public.profiles enable row level security;
alter table public.demo_identities enable row level security;
alter table public.group_context_items enable row level security;
revoke all on public.rooms, public.room_members, public.game_sessions, public.rounds,
  public.timeline_events, public.round_responses, public.demo_identities,
  public.group_context_items, public.profiles from public, anon, authenticated;
grant select on public.rooms, public.room_members, public.game_sessions, public.rounds,
  public.timeline_events, public.profiles to authenticated;
-- The existing asyncpg backend uses the trusted database role, not client RLS.
grant all on public.rooms, public.room_members, public.game_sessions, public.rounds,
  public.timeline_events, public.round_responses, public.demo_identities,
  public.group_context_items, public.profiles to service_role;

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

-- Explicit tables only; private state must never enter this publication.
do $$
declare t text;
begin
  if not exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    create publication supabase_realtime;
  end if;
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime' and puballtables) then
    raise exception 'supabase_realtime must not publish ALL TABLES';
  end if;
  foreach t in array array['round_responses', 'group_context_items', 'demo_identities'] loop
    if exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime'
               and schemaname = 'public' and tablename = t) then
      execute format('alter publication supabase_realtime drop table public.%I', t);
    end if;
  end loop;
  foreach t in array array['rooms', 'room_members', 'game_sessions', 'rounds', 'timeline_events'] loop
    if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime'
                   and schemaname = 'public' and tablename = t) then
      execute format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end $$;
commit;
