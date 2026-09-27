"""Opt-in real database tests. Requires BOTH migrations on a disposable LOCAL Supabase DB.

No migrations/reset are run here. All test data is rolled back. Never uses DATABASE_URL.
"""

import json
import os
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.services.game import GameService


@pytest_asyncio.fixture
async def database():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to an explicitly prepared local Supabase database")
    if urlparse(dsn).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("Database tests refuse remote hosts")
    db = await asyncpg.connect(dsn)
    for typename in ("json", "jsonb"):
        await db.set_type_codec(
            typename, schema="pg_catalog", encoder=json.dumps, decoder=json.loads, format="text"
        )
    transaction = db.transaction(isolation="repeatable_read")
    await transaction.start()
    try:
        users = [uuid4() for _ in range(3)]
        profiles = [uuid4() for _ in range(3)]
        for user, profile in zip(users, profiles):
            await db.execute("INSERT INTO auth.users(id) VALUES($1)", user)
            await db.execute(
                "INSERT INTO profiles(id,display_name) VALUES($1,$2)", profile, f"test-{profile}"
            )
            await GameService(db).bind_identity(user, profile)
        room = await GameService(db).create_room(users[0], "Security test")
        await GameService(db).join(users[1], room["join_code"])
        yield db, users, profiles, room
    finally:
        await transaction.rollback()
        await db.close()


@pytest.mark.asyncio
async def test_real_three_round_flow_and_privacy(database):
    db, users, profiles, room = database
    service = GameService(db)
    with pytest.raises(HTTPException):
        await service.hydrate(users[2], room["id"])
    with pytest.raises(HTTPException):
        await service.bind_identity(users[0], profiles[1])
    result = await service.start(users[0], room["id"])
    seen = set()
    for ordinal in range(1, 4):
        current = result["current_round"]
        round_id = current["id"]
        from uuid import UUID

        round_id = UUID(round_id)
        assert current["ordinal"] == ordinal and current["phase"] == "answering"
        assert current["reveal"] is None
        assert not {"answer", "value", "source_item_ids", "reveal_copy"} & current.keys()
        assert current["media"]["asset_key"] not in seen
        seen.add(current["media"]["asset_key"])
        secret = await db.fetchrow(
            "SELECT * FROM private.round_secrets WHERE round_id=$1", round_id
        )
        with pytest.raises(HTTPException):
            await service.reveal(users[0], round_id)
        with pytest.raises(HTTPException):
            await service.submit(users[2], round_id, profiles[0])
        for user in users[:2]:
            await service.submit(user, round_id, profiles[0])
            with pytest.raises(HTTPException):
                await service.submit(user, round_id, profiles[0])
        stored = await db.fetchrow("SELECT * FROM rounds WHERE id=$1", round_id)
        assert set(stored["submitted_profile_ids"]) == set(profiles[:2])
        assert stored["revision"] == 3 if ordinal == 1 else stored["revision"] == 4
        with pytest.raises(HTTPException):
            await service.reveal(users[1], round_id)
        reveal = await service.reveal(users[0], round_id)
        assert reveal["reveal"]["correct_profile_id"] == secret["answer"]["correct_profile_id"]
        assert reveal["phase"] == "revealed"
        with pytest.raises(HTTPException):
            await service.reveal(users[0], round_id)
        with pytest.raises(HTTPException):
            await service.advance(users[1], round_id)
        await db.execute(
            "UPDATE rounds SET revealed_at=now()-interval '10 minutes', nudges=1 WHERE id=$1",
            round_id,
        )
        result = await service.advance(users[0], round_id)
        with pytest.raises(HTTPException):
            await service.advance(users[0], round_id)
    assert result["session"]["status"] == "complete"
    snapshot = await service.hydrate(users[0], room["id"])
    assert snapshot["active_session"] is None and len(snapshot["rounds"]) == 3
    assert snapshot["last_session"]["status"] == "complete"
    event = await service.message(users[1], room["id"], "hello")
    assert event["event_type"] == "message" and event["actor_profile_id"] == profiles[1]


@pytest.mark.asyncio
async def test_actual_grants_rls_and_publication(database):
    db, users, _, room = database
    await GameService(db).start(users[0], room["id"])
    tables = {
        r["tablename"]
        for r in await db.fetch(
            "SELECT tablename FROM pg_publication_tables WHERE pubname='supabase_realtime'"
        )
    }
    assert {"rooms", "room_members", "game_sessions", "rounds", "timeline_events"} <= tables
    assert (
        not {"round_responses", "round_secrets", "demo_identities", "group_context_items"} & tables
    )
    columns = {
        r["column_name"]
        for r in await db.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='rounds'"
        )
    }
    assert not {"answer", "reveal_copy", "source_item_ids"} & columns
    for user, allowed in [(users[0], True), (users[2], False)]:
        async with db.transaction():
            await db.execute("SELECT set_config('request.jwt.claim.sub',$1,true)", str(user))
            await db.execute(
                "SELECT set_config('request.jwt.claims',$1,true)",
                json.dumps({"sub": str(user), "role": "authenticated"}),
            )
            await db.execute("SET LOCAL ROLE authenticated")
            for table in ["rooms", "room_members", "game_sessions", "rounds", "timeline_events"]:
                column = "id" if table == "rooms" else "room_id"
                rows = await db.fetch(f"SELECT * FROM public.{table} WHERE {column}=$1", room["id"])
                assert bool(rows) == allowed
                if table == "rounds" and allowed:
                    assert len(rows) == 1 and rows[0]["phase"] == "answering"
            for table in [
                "private.round_secrets",
                "public.round_responses",
                "public.demo_identities",
                "public.group_context_items",
            ]:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with db.transaction():
                        await db.fetch(f"SELECT * FROM {table}")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with db.transaction():
                    await db.execute(
                        "UPDATE public.rounds SET phase='revealed' WHERE room_id=$1", room["id"]
                    )
            await db.execute("RESET ROLE")


@pytest.mark.asyncio
async def test_automatic_three_round_flow(database):
    """Real SQL/JSON/visibility regression for the timer and HTTP service together."""
    from pathlib import Path
    from uuid import UUID

    from app.ai.conductor import Action, Conductor
    from app.services.game_master import tick_room
    from tests.test_game_master import pool_for

    db, users, profiles, room = database
    service = GameService(db)
    conductor = Conductor(Path("/missing"))
    pool = pool_for(db)
    await service.start(users[0], room["id"])
    for ordinal in range(1, 4):
        snapshot = await service.hydrate(users[0], room["id"])
        assert len(snapshot["rounds"]) == ordinal
        assert snapshot["current_round"]["ordinal"] == ordinal
        round_id = UUID(snapshot["current_round"]["id"])
        assert await db.fetchval("SELECT opened_at IS NOT NULL FROM rounds WHERE id=$1", round_id)
        await service.submit(users[0], round_id, profiles[0])
        assert (await tick_room(pool, conductor, room["id"])).action == Action.WAIT
        await service.submit(users[1], round_id, profiles[0])
        assert (await tick_room(pool, conductor, room["id"])).action == Action.REVEAL
        assert (await tick_room(pool, conductor, room["id"])).action == Action.WAIT
        # Even beyond the former time cap, a new message prevents advancement.
        await db.execute(
            "UPDATE rounds SET revealed_at=now()-interval '10 minutes', nudges=1 WHERE id=$1",
            round_id,
        )
        await service.message(users[0], room["id"], "still talking")
        assert (await tick_room(pool, conductor, room["id"])).action == Action.WAIT
        await db.execute(
            "UPDATE timeline_events SET created_at=now()-interval '2 minutes' WHERE room_id=$1 AND event_type='message'",
            room["id"],
        )
        assert (await tick_room(pool, conductor, room["id"])).action == Action.NEXT_ROUND
        # Duplicate timer delivery cannot emit the next prompt twice.
        again = await tick_room(pool, conductor, room["id"])
        assert again is None if ordinal == 3 else again.action == Action.WAIT
        assert await db.fetchval(
            "SELECT count(*) FROM timeline_events WHERE room_id=$1 AND event_type='game_prompt'",
            room["id"],
        ) == min(ordinal + 1, 3)
    final = await service.hydrate(users[0], room["id"])
    assert final["active_session"] is None
    assert final["last_session"]["status"] == "complete"
    assert len(final["rounds"]) == 3
