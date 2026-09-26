# Instagram Games Backend

FastAPI validates commands and owns game transitions. Supabase Postgres stores
shared state; Supabase Postgres Changes delivers live updates to room members.
There is no in-memory mode or polling loop in this synchronization implementation.
AI/context generation is intentionally outside this task.

## Setup

Use Python 3.12+ and install the existing dependencies:

```sh
uv sync --all-groups
cp .env.example .env
```

Configure a disposable Supabase project or local Supabase stack. Apply the two
migrations **in order** using the project's normal migration workflow:

1. `supabase/migrations/202609260001_initial_schema.sql`
2. The later migrations through `supabase/migrations/202609260009_realtime_game_state.sql`

The second migration is new and must be applied before running this backend. It
copies/verifies secrets before removing public secret columns; existing duplicate
profile claims or active sessions cause it to fail rather than discard data.
Apply during a demo maintenance window, with old clients/backend disconnected.
Never reset a shared database to apply these changes. This repository does not
include a configured Supabase CLI project; a plain PostgreSQL server without
Supabase Auth roles/schema and Realtime is insufficient for the complete flow.

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
AI keys are not needed. Start:

```sh
uv run uvicorn app.main:app --reload
```

## Demo flow

Provision 2–6 fictional `profiles` through a trusted database setup (e.g. Maya,
Daniel, Roshan, Alex). Each client signs in through Supabase Auth, reads the profile
roster, and binds an unclaimed profile with `POST /v1/demo-sessions`. Bindings are
immutable and exclusive, so preserve user auth sessions between refreshes.

1. Host creates a room; friends join by code, or use an existing provisioned room.
2. Subscribe and hydrate following [the exact frontend contract](docs/realtime-contract.md).
3. Host starts `POST /v1/rooms/{id}/sessions` with `{"vibe":"chaos"}`.
4. Each player submits `POST /v1/rounds/{id}/responses` with `{"value":"option-profile-uuid"}`.
5. Host calls `/reveal` when all have submitted, then `/advance`.
6. Repeat through three rounds; final advance completes the session.
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
