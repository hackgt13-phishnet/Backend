-- A group chat thread owns exactly one room; members join by opening the thread.
alter table public.rooms add column if not exists thread_key text;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'rooms_thread_key_unique') then
    alter table public.rooms add constraint rooms_thread_key_unique unique (thread_key);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'rooms_thread_key_format') then
    alter table public.rooms add constraint rooms_thread_key_format
      check (thread_key is null or thread_key ~ '^[A-Za-z0-9_-]{1,64}$');
  end if;
end $$;
