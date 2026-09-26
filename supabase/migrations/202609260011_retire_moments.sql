-- A memory rebuild replaces moments. Old ones a played round points to can't be deleted, so they're retired:
-- kept for history, never picked again.

alter table moments add column if not exists retired_at timestamptz;
