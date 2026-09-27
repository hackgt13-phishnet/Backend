"""Mocked SQL service/HTTP contracts. These tests do NOT prove RLS or Realtime."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import current_user_id
from app.routes import game_service, router
from app.services.game import (
    IDLE_CLAIM_MINUTES,
    GameService,
    check_submission,
    decode_cursor,
    encode_cursor,
    public_round,
    reveal_result,
)
from app.services.round_fixture import build_rounds


@pytest.fixture
def state():
    a, b, room, session, round_id = [uuid4() for _ in range(5)]
    row = {
        "id": round_id,
        "room_id": room,
        "session_id": session,
        "ordinal": 1,
        "game_type": "who_sent_this",
        "phase": "answering",
        "prompt": "Who sent this?",
        "media": {"asset_key": "demo/reel"},
        "options": [{"profile_id": str(p), "label": name} for p, name in [(a, "A"), (b, "B")]],
        "required_response_count": 2,
        "submitted_profile_ids": [],
        "reveal": None,
        "revision": 1,
    }
    secret = {
        "answer": {"correct_profile_id": str(b)},
        "reveal_copy": "B sent it!",
        "eligible_profile_ids": [a, b],
    }
    session_row = {"id": session, "room_id": room, "current_round_ordinal": 1, "status": "active"}
    return a, b, row, secret, session_row


@pytest.fixture
def db():
    result = MagicMock()
    result.fetch = AsyncMock()
    result.fetchrow = AsyncMock()
    result.fetchval = AsyncMock()
    result.execute = AsyncMock()

    @asynccontextmanager
    async def transaction(**kwargs):
        yield

    result.transaction.side_effect = transaction
    return result


def rejected(code, func, *args):
    with pytest.raises(HTTPException) as error:
        func(*args)
    assert error.value.status_code == code


def test_public_answering_round_allowlist(state):
    _a, b, row, secret, _ = state
    row.update(answer=secret["answer"], reveal_copy="SECRET", value=str(b), responses=["SECRET"])
    public = public_round(row)
    assert not {"answer", "reveal_copy", "value", "responses"} & public.keys()
    assert public["reveal"] is None
    assert public["submitted_profile_ids"] == []


def test_fixture_has_three_distinct_media_and_private_answer(state):
    a, b, *_ = state
    drafts = build_rounds([{"id": a, "display_name": "A"}, {"id": b, "display_name": "B"}])
    assert len(drafts) == len({r["media"]["asset_key"] for r in drafts}) == 3
    assert all(d["answer"]["correct_profile_id"] in {str(a), str(b)} for d in drafts)
    assert all("correct_profile_id" not in d["media"] for d in drafts)


def test_submission_validation(state):
    a, b, row, secret, _ = state
    check_submission(row, secret, a, b)
    rejected(403, check_submission, row, secret, uuid4(), b)
    rejected(422, check_submission, row, secret, a, uuid4())
    row["submitted_profile_ids"] = [a]
    rejected(409, check_submission, row, secret, a, b)
    row["phase"] = "revealed"
    rejected(409, check_submission, row, secret, b, a)


def test_reveal_uses_secret_and_exact_eligibility(state):
    a, b, _, secret, _ = state
    rejected(409, reveal_result, secret, [{"profile_id": a, "value": str(b)}])
    rejected(
        409,
        reveal_result,
        secret,
        [{"profile_id": a, "value": str(b)}, {"profile_id": uuid4(), "value": str(b)}],
    )
    result = reveal_result(
        secret, [{"profile_id": a, "value": str(b)}, {"profile_id": b, "value": str(a)}]
    )
    assert result["correct_profile_id"] == str(b)
    assert [r["points"] for r in result["results"]] == [1, 0]
    assert all("value" not in r and "selected_profile_id" not in r for r in result["results"])


@pytest.mark.asyncio
async def test_identity_idempotent_and_not_reassignable(db, state):
    a, b, *_ = state
    db.fetchval.side_effect = [True, a]
    await GameService(db).bind_identity(uuid4(), a)
    assert "ON CONFLICT DO NOTHING" in db.execute.call_args.args[0]
    db.fetchval.side_effect = [True, a]
    with pytest.raises(HTTPException) as error:
        await GameService(db).bind_identity(uuid4(), b)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_identity_frees_only_idle_claims_on_that_profile(db, state):
    a, *_ = state
    user = uuid4()
    db.fetchval.side_effect = [True, a]
    await GameService(db).bind_identity(user, a)
    freeing, claiming = (c.args for c in db.execute.call_args_list)
    assert freeing[0].startswith("DELETE FROM demo_identities WHERE profile_id=$1 AND user_id<>$2")
    assert f"interval '{IDLE_CLAIM_MINUTES} minutes'" in freeing[0]
    assert freeing[1:] == (a, user)
    assert claiming[0].startswith("INSERT INTO demo_identities")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["hydrate", "message"])
async def test_nonmember_rejected(db, state, method):
    a, b, row, *_ = state
    db.fetchval.side_effect = [a, False]
    db.fetchrow.return_value = {"id": row["room_id"], "host_profile_id": b}
    args = [uuid4(), row["room_id"]] + (["hello"] if method == "message" else [])
    with pytest.raises(HTTPException) as error:
        await getattr(GameService(db), method)(*args)
    assert error.value.status_code == 403
    assert all(
        c.args[0].startswith("UPDATE demo_identities SET last_seen_at")
        for c in db.execute.call_args_list
    )


@pytest.mark.asyncio
async def test_nonmember_submission_rejected(db, state):
    a, b, row, *_ = state
    db.fetchval.side_effect = [row["room_id"], a, False]
    db.fetchrow.return_value = {"id": row["room_id"], "host_profile_id": b}
    with pytest.raises(HTTPException) as error:
        await GameService(db).submit(uuid4(), row["id"], b)
    assert error.value.status_code == 403
    db.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["reveal", "advance"])
async def test_nonhost_rejected(db, state, method):
    a, b, row, *_ = state
    db.fetchval.side_effect = [row["room_id"], a, True]
    db.fetchrow.return_value = {"id": row["room_id"], "host_profile_id": b}
    with pytest.raises(HTTPException) as error:
        await getattr(GameService(db), method)(uuid4(), row["id"])
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_submit_private_insert_and_public_status(db, state):
    a, b, row, secret, session = state
    service = GameService(db)
    service.round_access = AsyncMock(return_value=(a, row, session))
    updated = {**row, "submitted_profile_ids": [a], "revision": 2}
    db.fetchrow.side_effect = [secret, updated]
    service.event = AsyncMock()
    result = await service.submit(uuid4(), row["id"], b)
    assert db.execute.call_args.args[1:] == (row["id"], a, str(b))
    assert result["submitted_profile_ids"] == [str(a)]
    assert "value" not in result and "answer" not in result
    assert db.transaction.call_count == 1


@pytest.mark.asyncio
async def test_reveal_updates_phase_and_payload_together(db, state):
    a, b, row, secret, session = state
    service = GameService(db)
    service.round_access = AsyncMock(return_value=(a, row, session))
    responses = [{"profile_id": a, "value": str(b)}, {"profile_id": b, "value": str(b)}]
    db.fetch.return_value = responses
    updated = {
        **row,
        "phase": "revealed",
        "reveal": reveal_result(secret, responses),
        "revision": 4,
    }
    db.fetchrow.side_effect = [secret, updated]
    service.event = AsyncMock()
    result = await service.reveal(uuid4(), row["id"])
    assert "SET phase='revealed',revealed_at=now(),reveal=$2" in db.fetchrow.call_args.args[0]
    assert result["phase"] == "revealed" and result["reveal"]["correct_profile_id"] == str(b)
    service.round_access.return_value = (a, updated, session)
    with pytest.raises(HTTPException) as error:
        await service.reveal(uuid4(), row["id"])
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_early_reveal_has_no_write(db, state):
    a, _, row, secret, session = state
    service = GameService(db)
    service.round_access = AsyncMock(return_value=(a, row, session))
    db.fetchrow.return_value = secret
    db.fetch.return_value = []
    with pytest.raises(HTTPException) as error:
        await service.reveal(uuid4(), row["id"])
    assert error.value.status_code == 409
    assert all("UPDATE" not in c.args[0] for c in db.fetchrow.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("last", [False, True])
async def test_advance_and_completion(db, state, last, monkeypatch):
    from app.ai.conductor import Action, Conductor, Decision
    from app.services import game_master

    monkeypatch.setattr(
        game_master, "load_state", AsyncMock(return_value=(None, state[2]["id"], state[4]["id"]))
    )
    monkeypatch.setattr(
        Conductor, "decide", lambda self, state: Decision(Action.NEXT_ROUND, "quiet")
    )
    a, _, row, _, session = state
    service = GameService(db)
    service.round_access = AsyncMock(return_value=(a, row, session))
    with pytest.raises(HTTPException) as error:
        await service.advance(uuid4(), row["id"])
    assert error.value.status_code == 409
    row["phase"] = "revealed"
    completed = {**row, "phase": "complete"}
    next_round = {**row, "id": uuid4(), "ordinal": 2, "phase": "pending"}
    new_session = {
        **session,
        "status": "complete" if last else "active",
        "current_round_ordinal": 1 if last else 2,
    }
    db.fetchrow.side_effect = (
        [completed, None, new_session]
        if last
        else [completed, next_round, {**next_round, "phase": "answering"}, new_session]
    )
    service.event = AsyncMock()
    result = await service.advance(uuid4(), row["id"])
    assert result["session"] == new_session
    assert result["current_round"]["phase"] == ("complete" if last else "answering")


@pytest.mark.asyncio
async def test_chat_uses_verified_actor_and_fixed_event(db, state):
    a, _, row, *_ = state
    service = GameService(db)
    service.room_access = AsyncMock(return_value=(a, {}))
    service.event = AsyncMock(return_value={"event_type": "message"})
    await service.message(uuid4(), row["room_id"], "hello")
    service.event.assert_awaited_once_with(row["room_id"], "message", {"body": "hello"}, a)


def test_http_rejects_actor_and_event_forgery(state):
    a, b, row, *_ = state
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    service = AsyncMock()
    app.dependency_overrides[current_user_id] = lambda: a
    app.dependency_overrides[game_service] = lambda: service
    with TestClient(app) as client:
        response = client.post(
            f"/v1/rounds/{row['id']}/responses", json={"value": str(b), "profile_id": str(b)}
        )
        assert response.status_code == 422
        response = client.post(
            f"/v1/rooms/{row['room_id']}/messages",
            json={"body": "hello", "event_type": "game_reveal"},
        )
        assert response.status_code == 422
        service.submit.return_value = {"accepted": True}
        assert (
            client.post(f"/v1/rounds/{row['id']}/responses", json={"value": str(b)}).status_code
            == 201
        )
        service.submit.assert_awaited_once_with(a, row["id"], str(b), None)


def test_timeline_cursor():
    row = {"created_at": datetime.now(UTC), "id": uuid4()}
    assert decode_cursor(encode_cursor(row)) == (row["created_at"], row["id"])
    rejected(422, decode_cursor, "not-a-cursor")


@pytest.mark.asyncio
async def test_start_creates_public_and_private_rows_in_one_transaction(db, state):
    a, b, row, _, session = state
    service = GameService(db)
    service.room_access = AsyncMock(return_value=(a, {"id": row["room_id"]}))
    service.event = AsyncMock()
    db.fetchval.return_value = False
    db.fetch.return_value = [{"id": a, "display_name": "A"}, {"id": b, "display_name": "B"}]
    db.fetchrow.side_effect = [session, row, {**row, "id": uuid4()}, {**row, "id": uuid4()}]
    result = await service.start(uuid4(), row["room_id"])
    assert result["current_round"]["reveal"] is None
    assert db.transaction.call_count == 1
    secret_inserts = [
        call for call in db.execute.call_args_list if "private.round_secrets" in call.args[0]
    ]
    answer_inserts = [call for call in db.execute.call_args_list if "round_answers" in call.args[0]]
    assert len(secret_inserts) == 3 and len(answer_inserts) == 3
    assert "INSERT INTO game_sessions" in db.fetchrow.call_args_list[0].args[0]
    public_inserts = db.fetchrow.call_args_list[1:]
    assert [call.args[4] for call in public_inserts] == ["answering", "pending", "pending"]
    assert all(
        "answer" not in call.args[0].split("VALUES")[0] and "reveal_copy" not in call.args[0]
        for call in public_inserts
    )
    db.fetchval.return_value = True
    with pytest.raises(HTTPException) as error:
        await service.start(uuid4(), row["room_id"])
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_round_access_locks_room_before_round_and_resolves_actor(db, state):
    a, b, row, _, session = state
    db.fetchval.side_effect = [row["room_id"], a, True]
    db.fetchrow.side_effect = [{"id": row["room_id"], "host_profile_id": a}, row, session]
    actor, current, _ = await GameService(db).round_access(row["id"], b)
    assert actor == a and current == row
    queries = [c.args[0] for c in db.fetchrow.call_args_list]
    assert "rooms" in queries[0] and "FOR UPDATE" in queries[0]
    assert "rounds" in queries[1] and "FOR UPDATE" in queries[1]
    assert db.fetchval.call_args_list[1].args[1] == b  # JWT user used in identity lookup


@pytest.mark.asyncio
async def test_hydrate_uses_consistent_public_snapshot(db, state):
    a, b, row, _, session = state
    row["submitted_profile_ids"] = [a]
    service = GameService(db)
    service.room_access = AsyncMock(return_value=(a, {"id": row["room_id"]}))
    db.fetchrow.return_value = session
    db.fetch.side_effect = [[{"profile_id": a}, {"profile_id": b}], [row]]
    service.timeline_page = AsyncMock(return_value={"events": [], "next_cursor": None})
    snapshot = await service.hydrate(uuid4(), row["room_id"])
    db.transaction.assert_called_once_with(isolation="repeatable_read", readonly=True)
    assert snapshot["viewer_has_submitted"]
    assert snapshot["current_round"]["reveal"] is None
    assert snapshot["active_session"] == session
    assert "phase<>'pending'" in db.fetch.call_args.args[0]
    assert all("round_responses" not in c.args[0] for c in db.fetch.call_args_list)


def test_real_jwt_validation_without_network():
    import time

    import jwt
    from fastapi.security import HTTPAuthorizationCredentials

    from app.auth import current_user_id
    from app.config import Settings

    actor = uuid4()
    key = "test-secret-for-local-verification-only-at-least-32-bytes"
    settings = Settings(
        _env_file=None,
        database_url="unused",
        supabase_jwt_secret=key,
        supabase_jwt_issuer="https://example.test/auth/v1",
        supabase_jwks_url="",
    )
    claims = {
        "sub": str(actor),
        "aud": "authenticated",
        "role": "authenticated",
        "iss": settings.supabase_jwt_issuer,
        "exp": int(time.time()) + 60,
    }

    def credentials(data, signing_key=key):
        return HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=jwt.encode(data, signing_key, algorithm="HS256")
        )

    assert current_user_id(credentials(claims), settings) == actor
    for invalid in [
        {**claims, "role": "service_role"},
        {**claims, "exp": 1},
        {**claims, "iss": "https://wrong.test"},
        {**claims, "aud": "other"},
    ]:
        rejected(401, current_user_id, credentials(invalid), settings)
    rejected(401, current_user_id, credentials(claims, key + "wrong"), settings)


@pytest.mark.asyncio
async def test_host_advance_cannot_bypass_conversation_gate(db, state, monkeypatch):
    from app.ai.conductor import Action, Conductor, Decision
    from app.services import game_master

    a, _, row, _, session = state
    row["phase"] = "revealed"
    service = GameService(db)
    service.round_access = AsyncMock(return_value=(a, row, session))
    monkeypatch.setattr(
        game_master, "load_state", AsyncMock(return_value=(None, row["id"], session["id"]))
    )
    monkeypatch.setattr(
        Conductor, "decide", lambda self, state: Decision(Action.WAIT, "someone just spoke")
    )
    with pytest.raises(HTTPException) as error:
        await service.advance(uuid4(), row["id"])
    assert error.value.status_code == 409
    db.fetchrow.assert_not_called()
