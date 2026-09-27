begin;
create table public.demo_threads (
  thread_key text primary key check (thread_key ~ '^[A-Za-z0-9_-]{1,100}$'),
  room_id uuid unique references public.rooms(id) on delete cascade
);
alter table public.demo_threads enable row level security;
revoke all on public.demo_threads from public, anon, authenticated;
grant all on public.demo_threads to service_role;
alter table public.rounds add column if not exists player_profile_ids uuid[] not null default '{}';
update public.rounds r set player_profile_ids=s.eligible_profile_ids
from private.round_secrets s where s.round_id=r.id;
alter publication supabase_realtime drop table public.rounds;
alter publication supabase_realtime add table public.rounds (
 id,room_id,session_id,ordinal,game_type,phase,prompt,options,media,
 required_response_count,submitted_profile_ids,reveal,revision,player_profile_ids
);
commit;
