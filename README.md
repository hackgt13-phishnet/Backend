# Instagram Games Backend

FastAPI validates commands and owns game transitions. Supabase Postgres stores
shared state; Supabase Postgres Changes delivers live updates to room members.
There is no in-memory mode or polling loop in this synchronization implementation.
AI/context generation is intentionally outside this task.

## Run from scratch

The playable UI lives in [hackgt13-phishnet/frontend](https://github.com/hackgt13-phishnet/frontend)
(see its `GAMES.md`). This repo is the API it talks to.

1. Install (Python 3.12+). With uv: `uv sync --all-groups`. Without uv:

   ```sh
   python3 -m venv .venv
   .venv/bin/pip install asyncpg fastapi httpx numpy pydantic-settings "pyjwt[crypto]" scikit-learn "uvicorn[standard]" pytest pytest-asyncio ruff
   ```

2. `cp .env.example .env` and fill it in. `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`
   and `META_MUSE_API_KEY` are required for AI rounds (`FALLBACK_LLM_*` is used when Muse fails).
   Without an AI key, games still run on the placeholder rounds in `round_fixture.py`.

3. Database. Using the team's existing Supabase project: nothing to do, it is migrated and seeded.
   For a **new** Supabase project:
   - Authentication → Sign In / Providers → enable **anonymous sign-ins**.
   - Apply every migration in order (safe to re-run, skips applied ones):
     `uv run --env-file .env python scripts/migrate.py`
   - Load the demo friend group (Maya, Dev, Sam, Ana, Kofi, Riya):

     ```sh
     uv run --env-file .env --group ml python scripts/build_memory.py --write-db   # profiles, group chat, moments
     uv run --env-file .env python scripts/seed_activity.py --write-db             # posts, stories, likes → interests
     uv run --env-file .env python scripts/add_post_photos.py --write-db           # optional, needs UNSPLASH_ACCESS_KEY
     ```

   - Point the frontend's `js/games-config.js` at the new project URL and publishable key.

4. Start: `GAME_PACE=demo uv run uvicorn app.main:app --host 127.0.0.1 --port 8001`
   (or `.venv/bin/python -m uvicorn ...`). `curl localhost:8001/v1/health` → `{"status":"ok"}`.

A plain PostgreSQL server without Supabase Auth roles/schema and Realtime is insufficient for
the complete flow. Never reset a shared database to apply migrations.

## Security notes

Set `DATABASE_URL` to the **server-only trusted PostgreSQL connection** used by
asyncpg (migration owner/postgres, or an explicitly granted privileged backend
role). It needs access to private secrets. End-user RLS does not substitute for
the backend's membership checks. Keep the `private` schema out of Supabase's
exposed Data API schemas.

Configure JWT verification for the SAME Supabase project:

- Legacy HS256: set `SUPABASE_JWT_SECRET`.
- Asymmetric signing: set `SUPABASE_JWKS_URL` to the project's Auth JWKS endpoint;
  this takes precedence and accepts ES256/RS256, not HS256.
- Set `SUPABASE_JWT_ISSUER` to the project's `/auth/v1` issuer.

Tokens require `sub`, `exp`, audience `authenticated`, and role `authenticated`.
Supabase anonymous sign-ins work; a publishable key alone is not a user token.

## Demo flow

Provision 2–6 fictional `profiles` through a trusted database setup (e.g. Maya,
Daniel, Roshan, Alex). Each client signs in through Supabase Auth, reads the profile
roster, and binds an unclaimed profile with `POST /v1/demo-sessions`. Bindings are
immutable and exclusive, so preserve user auth sessions between refreshes.

1. Host creates a room; friends join by code, or use an existing provisioned room.
2. Subscribe and hydrate following [the exact frontend contract](docs/realtime-contract.md).
3. Host starts `POST /v1/rooms/{id}/sessions` with `{"vibe":"chaos"}`.
4. Each player submits `POST /v1/rounds/{id}/responses` with `{"value":"option-profile-uuid"}`.
5. The game master reveals after every eligible player answers, then waits for the conversation to wind down.
6. It opens rounds 2 and 3 one at a time; after the final discussion it completes the session.
   The frontend does not need to call `/advance`; premature requests return 409.
7. Send chat through `POST /v1/rooms/{id}/messages` with `{"body":"hello"}`.

The fixture contains synthetic sender assignments and three `sync-demo/reel-N`
asset keys. The frontend supplies placeholder visuals/assets. No real Instagram
history or LLM is involved. Replace `app/services/round_fixture.py` with the game
team's trusted generation interface later; never accept answers from the client.

Public Realtime tables: `rooms`, `room_members`, `game_sessions`, `rounds`,
`timeline_events`. Private/unpublished: responses, secrets, context, identities.
Phase and reveal data change in the same public round UPDATE. Submission status
comes from `rounds.submitted_profile_ids`, not response-table subscriptions.

## Verification

```sh
uv run pytest -q
uv run ruff check app tests scripts
uv run ruff format --check app tests scripts
```

Unit/HTTP tests mock SQL and do not prove RLS or Realtime. Opt-in database tests
require BOTH migrations on a **disposable local Supabase database**, refuse remote
hosts, and roll back test fixtures. They do not migrate/reset a database:

```sh
TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:54322/postgres' uv run pytest -q tests/test_database_security.py
```

The live harness uses `httpx` and `websockets` (already supplied by
`uvicorn[standard]`). It does not run by default. Configure these explicitly for
an authorized disposable environment:

```text
VERIFY_API_URL=http://127.0.0.1:8000
VERIFY_SUPABASE_URL=http://127.0.0.1:54321
VERIFY_PUBLISHABLE_KEY=<client publishable or anon key>
VERIFY_TOKEN_A=<host user access token>
VERIFY_TOKEN_B=<member user access token>
VERIFY_TOKEN_C=<outsider user access token>
VERIFY_PROFILE_A=<distinct existing profile UUID>
VERIFY_PROFILE_B=<distinct existing profile UUID>
VERIFY_PROFILE_C=<distinct existing profile UUID>
```

Then explicitly authorize the harness to create a room/session/messages:

```sh
uv run python scripts/verify_realtime.py --run
```

It verifies two clients, three rounds, status/reveal/advance/chat, private response
read denial, and outsider isolation. It leaves fixture rows for inspection and
never migrates or resets. Obtain approval before using any remote project.
