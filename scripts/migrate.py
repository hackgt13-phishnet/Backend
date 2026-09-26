"""Apply supabase/migrations/*.sql in order, once each. Safe to re-run.

uv run --env-file .env python scripts/migrate.py
"""

import asyncio
import os
from pathlib import Path

import asyncpg

MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=20)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        await conn.execute("ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY")
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                print(f"  skip   {path.name}")
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations(name) VALUES($1)", path.name)
            print(f"  apply  {path.name}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
