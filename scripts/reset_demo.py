"""Get the shared local DB ready for a demo recording. Safe to re-run. Refuses remote hosts.

Frees every demo profile claim, ends games still running in other chats, and deletes
Chaos sessions in the Instagram demo threads so Send Chaos starts a new game.

    cd ~/Desktop/Coding/Backend && .venv/bin/python scripts/reset_demo.py
"""

import asyncio
import os
from urllib.parse import urlparse

import asyncpg
from dotenv import load_dotenv

DEMO_THREADS = ("group-shourya", "roshan-group")


def require_local(dsn: str) -> None:
    host = urlparse(dsn).hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing non-local database")


async def main() -> None:
    load_dotenv()
    dsn = os.environ["DATABASE_URL"]
    require_local(dsn)
    conn = await asyncpg.connect(dsn, timeout=20, statement_cache_size=0)
    try:
        async with conn.transaction():
            freed = await conn.execute("DELETE FROM demo_identities")
            cleared = await conn.execute(
                """DELETE FROM game_sessions
                   WHERE room_id IN (SELECT id FROM rooms WHERE thread_key = ANY($1::text[]))""",
                list(DEMO_THREADS),
            )
            await conn.execute(
                """DELETE FROM timeline_events
                   WHERE room_id IN (SELECT id FROM rooms WHERE thread_key = ANY($1::text[]))""",
                list(DEMO_THREADS),
            )
            ended = await conn.execute(
                """UPDATE game_sessions SET status = 'complete'
                   WHERE status = 'active'
                     AND room_id IN (
                       SELECT id FROM rooms
                       WHERE thread_key IS NOT NULL AND NOT (thread_key = ANY($1::text[]))
                     )""",
                list(DEMO_THREADS),
            )
            # Players rejoin by opening the chat, so only phones in the room are dealt into the next game.
            left = await conn.execute(
                """UPDATE room_members SET left_at = now()
                   WHERE left_at IS NULL
                     AND room_id IN (SELECT id FROM rooms WHERE thread_key IS NOT NULL)"""
            )
        print(f"profile claims freed: {freed.split()[-1]}")
        print(f"demo chat games removed: {cleared.split()[-1]}")
        print(f"other running chat games ended: {ended.split()[-1]}")
        print(f"chat memberships cleared: {left.split()[-1]}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
