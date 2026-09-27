# Realtime game-state contract

FastAPI is the only application writer. Supabase Postgres is authoritative. Clients
use Supabase Auth, call FastAPI with `Authorization: Bearer <access_token>`, and
consume Postgres Changes with their own Supabase user session. Never put a database
password or service-role key in the frontend.

## Bootstrap and identities

Authenticate with Supabase (anonymous sign-in is sufficient). Read the fictional
roster from `supabase.from('profiles').select('id,display_name,avatar_url')`.
`POST /v1/demo-sessions {"profile_id":"UUID"}` binds the authenticated user and
returns 204. The same binding is idempotent; changing it or claiming an occupied
profile returns 409. Preserve the auth session across app refreshes. Demo profile
claiming is not real-world identity verification.

Create a room with `POST /v1/rooms {"name":"Friends"}` or join using
`POST /v1/rooms/join {"code":"JOINCODE"}`. Existing conversations can use an
already-provisioned room ID and membership; creating a separate room is not
required by game commands. IDs everywhere below are UUID strings.

## Subscribe first, then hydrate

1. Authenticate and resolve/join the room.
2. Register the handlers below on one channel, named `room:${roomId}`.
3. Buffer events and wait for `SUBSCRIBED`.
4. `GET /v1/rooms/{roomId}`.
5. Install the snapshot, keyed by table primary key.
6. Merge buffered mutable rows only when their revision exceeds the stored row's
   revision; insert absent rows. Merge timeline events by ID.
7. Process future changes with the same reducer.

On reconnect, channel error, app resume, or expired-token recovery, repeat this
process. Keep Supabase's Realtime auth token refreshed along with the Auth session.
No polling is required for ordinary live updates. A disconnected client cannot
assume it received every transition.

```js
const channel = supabase.channel(`room:${roomId}`);
const handlers = [
  ['rooms', 'UPDATE', `id=eq.${roomId}`],
  ['room_members', 'INSERT', `room_id=eq.${roomId}`],
  ['room_members', 'UPDATE', `room_id=eq.${roomId}`],
  ['game_sessions', 'INSERT', `room_id=eq.${roomId}`],
  ['game_sessions', 'UPDATE', `room_id=eq.${roomId}`],
  ['rounds', 'INSERT', `room_id=eq.${roomId}`],
  ['rounds', 'UPDATE', `room_id=eq.${roomId}`],
  ['timeline_events', 'INSERT', `room_id=eq.${roomId}`],
];
for (const [table, event, filter] of handlers) {
  channel.on('postgres_changes', { schema: 'public', table, event, filter },
    payload => bufferOrApply(table, payload.new));
}
channel.subscribe(status => {
  if (status === 'SUBSCRIBED') hydrateAndReconcile(roomId);
});
// On room change, logout, or component disposal:
// await supabase.removeChannel(channel);
```

`bufferOrApply` and `hydrateAndReconcile` are frontend reducer hooks, not SDK APIs.
Cancel obsolete hydration requests when leaving a room. Coalesce repeated
SUBSCRIBED callbacks so only the newest hydration installs its result.

## Subscription payloads

All subscriptions use schema `public` and `payload.new`. Filters limit delivery;
RLS enforces room authorization even if a client removes the filter.

| Table | Events / filter | Payload and store update | Deduplication |
|---|---|---|---|
| `rooms` | UPDATE / `id=eq.{roomId}` | `id,name,join_code,host_profile_id,created_at,revision`; room metadata | `id` + revision |
| `room_members` | INSERT, UPDATE / `room_id=eq.{roomId}` | `room_id,profile_id,role,joined_at,left_at,revision`; roster | `(room_id,profile_id)` + revision |
| `game_sessions` | INSERT, UPDATE / `room_id=eq.{roomId}` | `id,room_id,vibe,status,created_at,current_round_ordinal,revision`; session/current pointer | `id` + revision |
| `rounds` | INSERT, UPDATE / `room_id=eq.{roomId}` | Public round below; prompt, status, phase, reveal | `id` + revision |
| `timeline_events` | INSERT / `room_id=eq.{roomId}` | `id,room_id,event_type,actor_profile_id,payload,created_at`; chat/game cards | `id`; immutable |

Profiles are static demo display data. Hydration includes names/avatars on member
rows; membership events contain profile IDs, resolved against the cached roster.
Treat `left_at != null` as departed; there is no DELETE subscription. A departing
user must exit the room locally after the command succeeds, because RLS can stop
its delivery immediately. Departures are rejected during active sessions; the
host cannot leave in this demo. A new joiner during a session may watch/chat but
is not eligible for rounds whose participant snapshot was already created.

**Do not subscribe to `round_responses`.** It is unpublished and unreadable to
clients. Never query secrets, raw context, or demo identity mapping internals.

## Public round

```json
{
  "id": "round-uuid",
  "room_id": "room-uuid",
  "session_id": "session-uuid",
  "ordinal": 1,
  "game_type": "who_sent_this",
  "phase": "answering",
  "prompt": "Who sent this reel?",
  "media": {"asset_key": "sync-demo/reel-1", "caption": "A dog stealing a picnic sandwich"},
  "options": [{"profile_id": "player-uuid", "label": "Maya"}],
  "required_response_count": 4,
  "submitted_profile_ids": [],
  "reveal": null,
  "revision": 1
}
```

The example abbreviates options. A real fixture round has 2–6 options. Fixture
media uses asset keys, not hosted videos; frontend can render placeholder cards
or map `sync-demo/reel-1`, `reel-2`, `reel-3` to controlled assets. Hydration may
include explicit null optional media fields. Neither media nor options identify
the correct answer.

```js
const answered = round.submitted_profile_ids.length;
const remaining = round.required_response_count - answered;
const iSubmitted = round.submitted_profile_ids.includes(viewerProfileId);
// `${answered} / ${round.required_response_count} answered`
// `Waiting on ${remaining} more responses...`
```

The array contains submission **status**, never choices. All participants present
at session start answer in this synthetic fixture. The sender is also a respondent;
this fixture exercises synchronization, not authentic historical gameplay.

The host can request reveal when the count is complete, but FastAPI rechecks
actual private responses and exact eligibility. One committed round UPDATE sets
both `phase: revealed` and:

```json
{
  "reveal": {
    "correct_profile_id": "player-uuid",
    "message": "Maya sent it!",
    "results": [{"profile_id": "respondent-uuid", "correct": true, "points": 1}]
  }
}
```

No individual selected answers are published, even after reveal. Correctness and
points are public. Points are stored once inside the reveal; there is no separate
mutable score ledger. Sum visible round results if a session total is needed.
Repeated reveal/advance requests return 409 and cannot award points twice.

Pending future rounds are denied by RLS. Treat an activation UPDATE for an unseen
round as an upsert. Do not render a new current round solely because another
round changed: use the session's `current_round_ordinal` and matching `session_id`.
Buffer an unresolved pointer until that round arrives. Database commits across
several tables are not delivered as one frontend event. The reveal itself is one
row update. Devices converge on the same reveal, not an exact wall-clock instant.

## HTTP commands

| Method/path | Body | Result |
|---|---|---|
| POST `/v1/demo-sessions` | `profile_id` | 204; 409 for conflicting binding |
| POST `/v1/rooms` | `name` | Room including join code |
| POST `/v1/rooms/join` | `code` | Room |
| POST `/v1/rooms/{id}/leave` | none | Updated membership |
| POST `/v1/rooms/{id}/sessions` | `{"vibe":"chaos","game_type":"who_sent_this"}` | `{session,current_round}`; host only |
| POST `/v1/rounds/{id}/responses` | `{"value":"option-profile-uuid"}` | Acceptance + status array/count/revision; duplicate 409 |
| POST `/v1/rounds/{id}/reveal` | none | Public revealed round; host only |
| POST `/v1/rounds/{id}/advance` | none | `{session,current_round}`; host only |
| POST `/v1/rooms/{id}/messages` | `{"body":"hello"}` | Public message event |

Extra command fields are rejected: clients cannot supply an actor or event type.
Missing membership is 403, absent resources 404, invalid phase/current round or
incomplete submissions 409, invalid input/option 422. After an uncertain request
outcome, hydrate instead of assuming a failed transport means no database commit.
No direct Supabase INSERT/UPDATE/DELETE application writes are permitted.

## Snapshot and timeline

`GET /v1/rooms/{id}` uses a repeatable-read transaction and returns:

```text
viewer_profile_id
room
members (including departed rows and display data)
active_session | null
last_session | null (latest completed session when none active)
rounds (non-pending rounds in selected session)
current_round | null
viewer_has_submitted
timeline (latest 50, oldest-to-newest)
timeline_cursor | null (older-history cursor)
```

`GET /v1/rooms/{id}/timeline?before={cursor}&limit=50` returns
`{events,next_cursor}`. The opaque cursor is for **older** history, not polling.
Order timeline by `(created_at,id)` and merge by event ID. The snapshot includes
current round submission status even if its latest status event is outside the
50-event history page.

Timeline game events (`game_started`, `game_prompt`, `submission_status`,
`game_reveal`) are historical cards. Do not let an old `game_prompt` card overwrite
current round state. Backend writes only safe public payloads.

Rows already revealed before migration retain a legacy `{answer,message,results}`
reveal shape. New fixture rounds always use the structured shape above. Existing
unrevealed legacy answers must be profile UUIDs or trusted
`{"correct_profile_id":"UUID"}` objects to resume reveal; other formats return
409 for a trusted data mapping rather than guessing the answer.


## Chaos round pacing

Starting a session opens round 1 and creates rounds 2 and 3 as `pending`.
Pending rounds must never render as chat cards. Hydration and the `member_rounds`
RLS policy (migration 009) exclude them. Use authenticated client credentials for
Realtime reads; do not use a service-role connection in the frontend.

The game master reveals only after the round's frozen eligible-player roster has
answered. There is no automatic answer timeout. It then allows discussion and
uses the conductor's lull prediction, minimum discussion time, and recent-message
guard before advancing. A story-holder nudge may precede advancement. There is no
maximum-discussion override: ongoing conversation keeps the next round pending.

Normal pace checks every 5 seconds, gives reveals at least 20 seconds, and never
advances within 15 seconds of a chat message. Demo pace checks every 2 seconds,
with 8-second discussion and recent-message guards. These are minimums, not fixed
advance timers; the silence model can wait longer.

Opening a round atomically updates its phase and opening time, the session's
`current_round_ordinal`, and a single `game_prompt` timeline event. Both automatic
and manual transitions use the same room lock, payload schema, and helpers.
`/reveal` and `/advance` remain host-only controls and enforce the same gates.
Premature `/advance` requests return 409 without changing any game state.
The frontend does not need to call either for automatic progression.

Render each game card once, keyed by the public round `id` in `game_prompt.payload`.
Apply round Realtime updates to that same card; do not append a second card for
those updates. Ignore `pending` rows defensively. On reconnect, hydrate the room
and reconcile by round ID and revision. Preserve already-opened rounds as chat
history. When the session completes, no fourth prompt is emitted.

If all three cards appear at session start, verify that migration 009 is applied,
that no privileged backend query is returning pending rows, and that the frontend
is not rendering the complete prefetched round list. This repository contains the
backend only; frontend rendering must follow this contract.
