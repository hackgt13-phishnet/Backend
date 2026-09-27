"""Exercise the live tick -> shared transition path using the API's decoded JSON contract."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.ai.conductor import Action, Conductor
from app.services.game import reveal_result
from app.services.game_master import load_state, tick_room
from tests import test_game_api

# Reuse the service fixtures without collecting its tests a second time.
db = test_game_api.db
state = test_game_api.state


def pool_for(db):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield db

    pool.acquire.side_effect = acquire
    return pool


def loaded(row, now=100, **changes):
    return {
        **row,
        "opened_at": datetime.fromtimestamp(1, UTC),
        "revealed_at": None,
        "now": now,
        "nudges": 1,
        "last_nudge_at": None,
        "story_holder_profile_id": None,
        **changes,
    }


@pytest.mark.asyncio
async def test_tick_reveals_native_json_only_after_all_eligible_answers(db, state):
    a, b, row, secret, _ = state
    responses = [{"profile_id": p, "value": str(b)} for p in (a, b)]
    updated = {**row, "phase": "revealed", "reveal": reveal_result(secret, responses)}
    db.fetchval.return_value = row["room_id"]
    db.fetch.side_effect = [[{"profile_id": a}, {"profile_id": b}], responses, [], responses]
    db.fetchrow.side_effect = [loaded(row), row, secret, updated, {}]
    decision = await tick_room(pool_for(db), Conductor(Path("/missing")), row["room_id"])
    assert decision.action == Action.REVEAL
    queries = [c.args[0] for c in db.fetchrow.call_args_list]
    assert any("revealed_at=now(),reveal=$2" in q for q in queries)
    event = db.fetchrow.call_args.args
    assert event[2] == "game_reveal"
    assert event[4]["reveal"] == updated["reveal"]
    assert "FOR UPDATE SKIP LOCKED" in db.fetchval.call_args.args[0]
    assert isinstance(db.execute.call_args.args[-1], dict)


@pytest.mark.asyncio
async def test_missing_answer_never_reveals_even_long_after_timeout(db, state):
    a, b, row, _, _ = state
    db.fetchval.return_value = row["room_id"]
    db.fetchrow.return_value = loaded(row, now=10000)
    db.fetch.side_effect = [[{"profile_id": a}, {"profile_id": b}], [{"profile_id": a}], []]
    decision = await tick_room(pool_for(db), Conductor(Path("/missing")), row["room_id"])
    assert decision.action == Action.WAIT
    assert db.fetchrow.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinal", [1, 2, 3])
async def test_quiet_tick_advances_atomically_or_completes(db, state, ordinal):
    a, b, base, _, session = state
    row = {**base, "phase": "revealed", "ordinal": ordinal}
    session = {**session, "current_round_ordinal": ordinal}
    next_row = {**base, "ordinal": ordinal + 1, "phase": "pending"} if ordinal < 3 else None
    completed = {**row, "phase": "complete"}
    new_session = {
        **session,
        "current_round_ordinal": min(ordinal + 1, 3),
        "status": "active" if next_row else "complete",
    }
    db.fetchval.return_value = row["room_id"]
    db.fetch.side_effect = [
        [{"profile_id": a}, {"profile_id": b}],
        [{"profile_id": a}, {"profile_id": b}],
        [],
    ]
    returns = [
        loaded(row, now=1000, revealed_at=datetime.fromtimestamp(10, UTC)),
        row,
        session,
        completed,
        next_row,
    ]
    returns += [{**next_row, "phase": "answering"}, new_session, {}] if next_row else [new_session]
    db.fetchrow.side_effect = returns
    decision = await tick_room(pool_for(db), Conductor(Path("/missing")), row["room_id"])
    assert decision.action == Action.NEXT_ROUND
    calls = db.fetchrow.call_args_list
    prompts = [c for c in calls if c.args[0].startswith("INSERT INTO timeline_events")]
    assert len(prompts) == (1 if next_row else 0)
    if next_row:
        assert prompts[0].args[4]["ordinal"] == ordinal + 1
        assert any("current_round_ordinal=$2" in c.args[0] for c in calls)
        assert any("opened_at=now()" in c.args[0] for c in calls)
    assert db.transaction.call_count == 1


@pytest.mark.asyncio
async def test_busy_room_is_skipped_before_read_or_write(db, state):
    db.fetchval.return_value = None
    assert await tick_room(pool_for(db), Conductor(Path("/missing")), state[2]["room_id"]) is None
    db.fetchrow.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_state_uses_frozen_roster_and_current_ordinal(db, state):
    a, b, row, _, _ = state
    db.fetchrow.return_value = loaded(row)
    db.fetch.side_effect = [[{"profile_id": a}, {"profile_id": b}], [], []]
    result = await load_state(db, row["room_id"])
    assert result[0].member_ids == frozenset((str(a), str(b)))
    assert "eligible_profile_ids" in db.fetch.call_args_list[0].args[0]
    assert "r.ordinal = s.current_round_ordinal" in db.fetchrow.call_args.args[0]
