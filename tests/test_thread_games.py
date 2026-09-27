from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.game import GameService
from app.services.threads import ThreadGames
from tests import test_game_api

db = test_game_api.db


@pytest.mark.asyncio
async def test_enroll_only_unrevealed_and_does_not_change_options(db):
    actor, host, session, room = [uuid4() for _ in range(4)]
    rounds = [
        {"id": uuid4(), "phase": phase, "eligible_profile_ids": [host]}
        for phase in ["revealed", "answering", "pending"]
    ]
    db.fetch.return_value = rounds
    await GameService(db).enroll(actor, room, {"id": session, "status": "active"})
    assert db.execute.call_count == 4
    assert {c.args[1] for c in db.execute.call_args_list} == {r["id"] for r in rounds[1:]}
    assert all(c.args[2] == [host, actor] for c in db.execute.call_args_list)
    assert all("options=" not in c.args[0] for c in db.execute.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,phase,count",
    [("complete", "complete", 1), ("active", "revealed", 1), ("active", "answering", 6)],
)
async def test_enrollment_rejects_closed_or_full(db, status, phase, count):
    db.fetch.return_value = [
        {"id": uuid4(), "phase": phase, "eligible_profile_ids": [uuid4() for _ in range(count)]}
    ]
    with pytest.raises(HTTPException) as error:
        await GameService(db).enroll(uuid4(), uuid4(), {"id": uuid4(), "status": status})
    assert error.value.status_code == 409
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_enrollment_is_noop_even_after_completion(db):
    actor = uuid4()
    db.fetch.return_value = [{"id": uuid4(), "phase": "complete", "eligible_profile_ids": [actor]}]
    await GameService(db).enroll(actor, uuid4(), {"id": uuid4(), "status": "complete"})
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_start_thread_reuses_standard_start_and_locks_mapping(db):
    service = ThreadGames(db)
    service.profile = AsyncMock(return_value=uuid4())
    service.create_room = AsyncMock(return_value={"id": uuid4()})
    service.start = AsyncMock(return_value={"session": {}})
    db.fetchval.return_value = None
    await service.start_thread(uuid4(), "group-shourya", "Friends")
    assert "FOR UPDATE" in db.fetchval.call_args.args[0]
    assert service.create_room.call_count == service.start.call_count == 1


@pytest.mark.asyncio
async def test_invite_join_rejects_session_from_other_room(db):
    service = ThreadGames(db)
    service.profile = AsyncMock(return_value=uuid4())
    db.fetchrow.side_effect = [{"id": uuid4()}, None]
    with pytest.raises(HTTPException) as error:
        await service.join_game(uuid4(), "group-shourya", uuid4())
    assert error.value.status_code == 404
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_discovery_exposes_metadata_without_answers(db):
    service = ThreadGames(db)
    actor = uuid4()
    service.profile = AsyncMock(return_value=actor)
    db.fetchrow.return_value = None
    db.fetch.return_value = []
    result = await service.games(uuid4(), "group-shourya")
    assert result == {"room_id": None, "can_start": True, "games": []}
    query = db.fetch.call_args.args[0]
    assert "LIMIT 50" in query
    assert "prompt" not in query and "reveal_copy" not in query
