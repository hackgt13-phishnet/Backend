-- GamePigeon-style sessions: every round opens at once, each player answers on their own time,
-- and a round reveals when its last eligible player answers. 'live' keeps the timed,
-- game-master-paced flow.
alter table public.game_sessions
  add column if not exists mode text not null default 'live';

alter type timeline_event_type add value if not exists 'game_over';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'game_sessions_mode_check') then
    alter table public.game_sessions
      add constraint game_sessions_mode_check check (mode in ('live', 'async'));
  end if;
end $$;
