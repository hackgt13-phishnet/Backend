"""Get the shared DB ready for a live multi-phone demo. Safe to re-run.

Frees every demo profile claim so each phone can pick one, and ends any game still
running in a chat so the first "Chaos" tap starts fresh. Past games are kept.

.venv/bin/python scripts/reset_demo.py
"""

import asyncio
import os

import asyncpg
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=20, statement_cache_size=0)
    try:
        async with conn.transaction():
            freed = await conn.execute("DELETE FROM demo_identities")
            ended = await conn.execute(
                """UPDATE game_sessions SET status = 'complete'
                   WHERE status = 'active'
                     AND room_id IN (SELECT id FROM rooms WHERE thread_key IS NOT NULL)"""
            )
            # Players rejoin by opening the chat, so only phones in the room are dealt into the next game.
            left = await conn.execute(
                """UPDATE room_members SET left_at = now()
                   WHERE left_at IS NULL
                     AND room_id IN (SELECT id FROM rooms WHERE thread_key IS NOT NULL)"""
            )
        print(f"profile claims freed: {freed.split()[-1]}")
        print(f"running chat games ended: {ended.split()[-1]}")
        print(f"chat memberships cleared: {left.split()[-1]}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
