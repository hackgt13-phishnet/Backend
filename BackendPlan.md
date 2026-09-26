nstagram Games Backend Plan

## Summary

Create a new Python/FastAPI backend repository deployed on Render, using Supabase for Postgres, Auth, Row Level Security, storage, and Realtime. The first production-quality demo flow supports live 2-5 player rooms, shareable room codes, room chat, and the Chaos vibe’s `Who Sent This?` and `Most Likely To` games.

The mobile app sends authenticated commands to FastAPI and subscribes directly to Supabase Realtime for room updates. FastAPI alone owns game-state transitions and Meta Muse generation; clients never reveal answers or calculate results themselves.

## Key implementation changes

- Create FastAPI modules for:
  - Demo identity: silently establish a Supabase anonymous session, then bind it to a selected seeded demo profile.
  - Rooms: create a room, generate a short unique join code, join by code, list members, and mark a host.
  - Timeline/chat: append and fetch typed room events, including normal messages, game prompt cards, submission indicators, and reveal cards.
  - Game sessions: start a Chaos session, create three rounds before play begins, accept one response per participant, and allow only the host to reveal or advance a round.
  - AI generation: retrieve curated context, compose a constrained model request, validate its output with Pydantic, and fall back to deterministic authored rounds on failure.

- Define these authoritative public APIs:
  - `POST /v1/demo-sessions` — bind the signed-in anonymous user to a seeded profile.
  - `POST /v1/rooms` and `POST /v1/rooms/join` — create or join by room code.
  - `GET /v1/rooms/{room_id}` and `GET /v1/rooms/{room_id}/timeline` — bootstrap a client after launch or reconnect.
  - `POST /v1/rooms/{room_id}/messages` — add a chat message.
  - `POST /v1/rooms/{room_id}/sessions` — host starts the three-round Chaos session.
  - `POST /v1/rounds/{round_id}/responses` — submit an immutable player answer.
  - `POST /v1/rounds/{round_id}/reveal` and `POST /v1/rounds/{round_id}/advance` — host-controlled phase changes.
  - Supabase Realtime subscription to room-scoped `timeline_events`, `rounds`, `round_responses`, and `room_members` changes.

- Add Postgres migrations for:
  - `profiles`, `rooms`, `room_members`, `game_sessions`, `rounds`, `round_responses`, `timeline_events`, and `group_context_items`.
  - A `rounds.phase` state machine: `pending → answering → revealed → complete`; FastAPI validates every transition.
  - A response visibility rule: individual responses remain hidden until the reveal transaction completes. Clients can see participant submission status only.
  - RLS policies scoped to room membership; FastAPI uses its server-only Supabase credential for privileged state changes.
  - Seed data for demo profiles, friendships, and a pre-curated, safe group history. Store media in a Supabase bucket and reference it from context items.

- Model context and generation:
  - Tag every seeded context item with content type, participants, timestamp, safety status, and theme labels.
  - Retrieve only 3-8 eligible items per round using deterministic filters: media-heavy items for `Who Sent This?`, recurring behavior/topic items for `Most Likely To`.
  - Send retrieved evidence, permitted player names, game type, and strict output instructions to Meta Model API’s Muse Spark through an OpenAI-compatible Python client.
  - Require JSON containing the prompt, options, answer/reference data, cited context-item IDs, and reveal copy; validate with Pydantic and reject any unknown source IDs or invalid player references.
  - Generate all three rounds at session start. On invalid output, timeout, or provider error, save an authored fallback round so play is uninterrupted.
  - Keep the provider behind an `LLMClient` interface so Meta Muse can be replaced without changing game logic. Meta’s Model API supports Muse Spark through an OpenAI-compatible integration. [Meta Model API overview](https://dev.meta.ai/resources/blog/build-with-muse-spark)

- Organize the new repository around `app/api`, `app/domain`, `app/services`, `app/repositories`, `app/schemas`, and `supabase/`. Include environment templates for Supabase, Meta Muse, and client origin settings, plus a Render deployment definition and health endpoint.

## Test plan

- Unit-test retrieval, round validation, scoring, fallback selection, and all allowed/forbidden state transitions.
- Integration-test room creation and code joining, authorization boundaries, response secrecy before reveal, host-only reveal/advance, and reconnect timeline hydration.
- Mock Meta Muse to test valid generation, malformed JSON, unsupported citations, timeouts, and fallback behavior.
- Verify two physical clients can join the same room and observe immediate chat, submitted-status, reveal, and next-round updates.
- Acceptance flow: select seeded profiles → create/join via code → choose Chaos → play three generated/fallback rounds → host reveals each round → continue chatting in the same timeline.

## Assumptions

- The frontend will use React Native/Expo and Supabase’s client library for anonymous authentication and Realtime subscriptions.
- “Demo profiles” are the visible identity mechanism; anonymous Supabase accounts are an implementation detail used to enforce room membership.
- The host explicitly controls when each answer phase reveals; there is no automatic timer-based reveal in MVP.
- No real Instagram data, persistent cross-room statistics, moderation system, or production-scale content ingestion is included. The curated mock-context approach and the core room/game flow follow the supplied PRD. :codex-file-citation{path="/Users/shreydesai/Downloads/HackGT 13 PRD.pdf" purpose="source"}
