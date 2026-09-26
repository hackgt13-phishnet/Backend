# Instagram Games Backend

FastAPI owns room commands and game-state transitions. Supabase provides Postgres, anonymous identity, row-level access control, and Realtime updates.

## Local setup

1. Create a Supabase project and apply `supabase/migrations/202609260001_initial_schema.sql`.
2. Copy `.env.example` to `.env` and add the database URL, Supabase JWT secret, and Meta Muse key.
3. Install dependencies with `uv sync --all-groups` and run `uv run uvicorn app.main:app --reload`.

The React Native client should authenticate anonymously with Supabase, select a seeded profile through `POST /v1/demo-sessions`, send commands to this API, and subscribe to its room tables over Supabase Realtime.
