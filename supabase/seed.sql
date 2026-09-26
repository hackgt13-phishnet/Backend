-- Fictional repeatable local fixtures. No credentials or answers in public rows.
insert into public.profiles(id, display_name) values
 ('10000000-0000-4000-8000-000000000001','Maya Demo'),
 ('10000000-0000-4000-8000-000000000002','Daniel Demo'),
 ('10000000-0000-4000-8000-000000000003','Alex Outsider'),
 ('10000000-0000-4000-8000-000000000004','Nia Demo')
on conflict(id) do update set display_name=excluded.display_name;
insert into public.rooms(id,name,join_code,host_profile_id) values
 ('20000000-0000-4000-8000-000000000001','Friday Picnic Club','PICNIC01','10000000-0000-4000-8000-000000000001'),
 ('20000000-0000-4000-8000-000000000002','Alex and Nia','OTHER001','10000000-0000-4000-8000-000000000003')
on conflict(id) do nothing;
insert into public.room_members(room_id,profile_id,role) values
 ('20000000-0000-4000-8000-000000000001','10000000-0000-4000-8000-000000000001','host'),
 ('20000000-0000-4000-8000-000000000001','10000000-0000-4000-8000-000000000002','member'),
 ('20000000-0000-4000-8000-000000000002','10000000-0000-4000-8000-000000000003','host'),
 ('20000000-0000-4000-8000-000000000002','10000000-0000-4000-8000-000000000004','member')
on conflict(room_id,profile_id) do nothing;
insert into public.timeline_events(id,room_id,event_type,actor_profile_id,payload,created_at) values
 ('30000000-0000-4000-8000-000000000001','20000000-0000-4000-8000-000000000001','message','10000000-0000-4000-8000-000000000001','{"body":"Friday picnic rematch? This time we check the weather."}','2026-09-20T15:00:00Z'),
 ('30000000-0000-4000-8000-000000000002','20000000-0000-4000-8000-000000000001','message','10000000-0000-4000-8000-000000000002','{"body":"The soggy sandwiches were part of the experience."}','2026-09-20T15:01:00Z'),
 ('30000000-0000-4000-8000-000000000003','20000000-0000-4000-8000-000000000001','message','10000000-0000-4000-8000-000000000001','{"body":"Bring Uno. Emergency cafe game night is officially our backup plan."}','2026-09-20T15:02:00Z'),
 ('30000000-0000-4000-8000-000000000004','20000000-0000-4000-8000-000000000002','message','10000000-0000-4000-8000-000000000003','{"body":"Our separate room should stay separate during the test."}','2026-09-20T16:00:00Z')
on conflict(id) do nothing;
-- No context extraction is needed by round_fixture.py. Game rounds are created
-- through the trusted start command so secrets follow the same production path.
