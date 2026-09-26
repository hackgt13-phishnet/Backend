import json
import secrets
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.ai.conductor import Action
from app.auth import current_user_id
from app.domain import (
    ROUNDS_PER_SESSION,
    CreateRoomRequest,
    DemoSessionRequest,
    JoinRoomRequest,
    MessageRequest,
    StartSessionRequest,
    SubmitResponseRequest,
)
from app.services.game_master import background, host_override, tick_room
from app.services.rounds import announce_round, draft_rounds, insert_round, room_members

router = APIRouter()


def pool(request: Request):
    return request.app.state.pool


async def profile_for_user(db, user_id: UUID) -> UUID:
    profile_id = await db.fetchval("SELECT profile_id FROM demo_identities WHERE user_id = $1", user_id)
    if not profile_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Choose a demo profile first")
    return profile_id


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/demo-sessions", status_code=status.HTTP_204_NO_CONTENT)
async def choose_demo_profile(
    payload: DemoSessionRequest, request: Request, user_id: UUID = Depends(current_user_id)
) -> None:
    async with pool(request).acquire() as db:
        exists = await db.fetchval("SELECT EXISTS(SELECT 1 FROM profiles WHERE id = $1)", payload.profile_id)
        if not exists:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demo profile not found")
        await db.execute(
            """INSERT INTO demo_identities(user_id, profile_id) VALUES($1, $2)
               ON CONFLICT (user_id) DO UPDATE SET profile_id = EXCLUDED.profile_id""",
            user_id,
            payload.profile_id,
        )


@router.post("/rooms", status_code=status.HTTP_201_CREATED)
async def create_room(
    payload: CreateRoomRequest, request: Request, user_id: UUID = Depends(current_user_id)
) -> dict:
    async with pool(request).acquire() as db:
        profile_id = await profile_for_user(db, user_id)
        for _ in range(5):
            code = secrets.token_urlsafe(5).upper()[:7]
            try:
                row = await db.fetchrow(
                    """INSERT INTO rooms(name, join_code, host_profile_id) VALUES($1, $2, $3)
                       RETURNING id, name, join_code, host_profile_id, created_at""",
                    payload.name,
                    code,
                    profile_id,
                )
                await db.execute(
                    "INSERT INTO room_members(room_id, profile_id, role) VALUES($1, $2, 'host')",
                    row["id"],
                    profile_id,
                )
                return dict(row)
            except asyncpg.UniqueViolationError:
                continue
    raise HTTPException(status_code=503, detail="Could not create a unique room code")


@router.post("/rooms/join")
async def join_room(
    payload: JoinRoomRequest, request: Request, user_id: UUID = Depends(current_user_id)
) -> dict:
    async with pool(request).acquire() as db:
        profile_id = await profile_for_user(db, user_id)
        room = await db.fetchrow("SELECT id, name, join_code FROM rooms WHERE join_code = $1", payload.code.upper())
        if not room:
            raise HTTPException(status_code=404, detail="Room code not found")
        await db.execute(
            """INSERT INTO room_members(room_id, profile_id) VALUES($1, $2)
               ON CONFLICT (room_id, profile_id) DO NOTHING""",
            room["id"],
            profile_id,
        )
        return dict(room)


@router.post("/rooms/{room_id}/messages", status_code=status.HTTP_201_CREATED)
async def post_message(
    room_id: UUID, payload: MessageRequest, request: Request, user_id: UUID = Depends(current_user_id)
) -> dict:
    async with pool(request).acquire() as db:
        profile_id = await profile_for_user(db, user_id)
        member = await db.fetchval(
            "SELECT EXISTS(SELECT 1 FROM room_members WHERE room_id = $1 AND profile_id = $2)", room_id, profile_id
        )
        if not member:
            raise HTTPException(status_code=403, detail="Not a room member")
        event = await db.fetchrow(
            """INSERT INTO timeline_events(room_id, event_type, actor_profile_id, payload)
               VALUES($1, 'message', $2, jsonb_build_object('body', $3)) RETURNING *""",
            room_id,
            profile_id,
            payload.body,
        )
    # Every message is a signal for the game master. It decides in the background, usually to wait.
    background(tick_room(pool(request), request.app.state.conductor, room_id))
    return dict(event)


async def require_member(db, room_id: UUID, user_id: UUID) -> UUID:
    profile_id = await profile_for_user(db, user_id)
    member = await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM room_members WHERE room_id = $1 AND profile_id = $2)", room_id, profile_id
    )
    if not member:
        raise HTTPException(status_code=403, detail="Not a room member")
    return profile_id


@router.post("/rooms/{room_id}/sessions", status_code=status.HTTP_201_CREATED)
async def start_session(
    room_id: UUID, payload: StartSessionRequest, request: Request, user_id: UUID = Depends(current_user_id)
) -> dict:
    async with pool(request).acquire() as db:
        profile_id = await require_member(db, room_id, user_id)
        if await db.fetchval("SELECT host_profile_id FROM rooms WHERE id = $1", room_id) != profile_id:
            raise HTTPException(status_code=403, detail="Only the room host can start a game")
        if await db.fetchval("SELECT EXISTS(SELECT 1 FROM game_sessions WHERE room_id = $1 AND status = 'active')", room_id):
            raise HTTPException(status_code=409, detail="A game is already running in this room")
        session_id = await db.fetchval(
            "INSERT INTO game_sessions(room_id, vibe) VALUES($1, $2) RETURNING id", room_id, payload.vibe
        )
    # All rounds are written up front, in parallel, so play never waits on the model mid-game.
    drafted = await draft_rounds(pool(request), room_id, session_id, list(range(1, ROUNDS_PER_SESSION + 1)))
    if not drafted:
        async with pool(request).acquire() as db:
            await db.execute("UPDATE game_sessions SET status = 'complete' WHERE id = $1", session_id)
        raise HTTPException(status_code=422, detail="Not enough shared history in this room for a game yet")
    async with pool(request).acquire() as db, db.transaction():
        round_ids = [
            await insert_round(db, session_id, ordinal, draft, "answering" if ordinal == 1 else "pending")
            for ordinal, draft in drafted
        ]
        round_id = round_ids[0]
        await db.execute(
            "INSERT INTO timeline_events(room_id, event_type, payload) VALUES($1, 'game_started', $2::jsonb)",
            room_id, json.dumps({"session_id": str(session_id), "vibe": payload.vibe, "rounds": len(drafted),
                                 "branch": drafted[0][1].branch}),
        )
        await announce_round(db, room_id, round_id)
    return {"session_id": str(session_id), "round_id": str(round_id)}


@router.post("/rounds/{round_id}/responses", status_code=status.HTTP_201_CREATED)
async def submit_response(
    round_id: UUID, payload: SubmitResponseRequest, request: Request, user_id: UUID = Depends(current_user_id)
) -> dict:
    async with pool(request).acquire() as db, db.transaction():
        rnd = await db.fetchrow(
            """SELECT r.phase, r.options, s.room_id FROM rounds r JOIN game_sessions s ON s.id = r.session_id
               WHERE r.id = $1 FOR UPDATE OF r""",
            round_id,
        )
        if not rnd:
            raise HTTPException(status_code=404, detail="Round not found")
        profile_id = await require_member(db, rnd["room_id"], user_id)
        if rnd["phase"] != "answering":
            raise HTTPException(status_code=409, detail="This round isn't taking answers")
        if payload.value not in json.loads(rnd["options"])["choices"]:
            raise HTTPException(status_code=422, detail="Pick one of the options")
        inserted = await db.fetchval(
            """INSERT INTO round_responses(round_id, profile_id, value) VALUES($1, $2, $3::jsonb)
               ON CONFLICT (round_id, profile_id) DO NOTHING RETURNING 1""",
            round_id, profile_id, json.dumps(payload.value),
        )
        if not inserted:
            raise HTTPException(status_code=409, detail="You already answered")
        answered = await db.fetchval("SELECT count(*) FROM round_responses WHERE round_id = $1", round_id)
        total = await db.fetchval("SELECT count(*) FROM room_members WHERE room_id = $1", rnd["room_id"])
        await db.execute(
            "INSERT INTO timeline_events(room_id, event_type, payload) VALUES($1, 'submission_status', $2::jsonb)",
            rnd["room_id"], json.dumps({"round_id": str(round_id), "answered": answered, "total": total}),
        )
    # The last answer usually triggers the reveal right away.
    background(tick_room(pool(request), request.app.state.conductor, rnd["room_id"]))
    return {"answered": answered, "total": total}


@router.get("/rooms/{room_id}/gm-decisions")
async def game_master_decisions(room_id: UUID, request: Request, user_id: UUID = Depends(current_user_id)) -> list:
    """Debug view: what the game master decided, and why. Newest first."""
    async with pool(request).acquire() as db:
        await require_member(db, room_id, user_id)
        rows = await db.fetch(
            """SELECT action, reason, p_silence, model_source, target_profile_id, features, created_at
               FROM gm_decisions WHERE room_id = $1 ORDER BY created_at DESC LIMIT 200""",
            room_id,
        )
        return [dict(r) for r in rows]



async def require_host(db, round_id: UUID, user_id: UUID) -> UUID:
    row = await db.fetchrow(
        """SELECT s.room_id, rm.host_profile_id FROM rounds r JOIN game_sessions s ON s.id = r.session_id
           JOIN rooms rm ON rm.id = s.room_id WHERE r.id = $1""",
        round_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Round not found")
    if await profile_for_user(db, user_id) != row["host_profile_id"]:
        raise HTTPException(status_code=403, detail="Only the room host can do that")
    return row["room_id"]


@router.post("/rounds/{round_id}/reveal")
async def reveal_round(round_id: UUID, request: Request, user_id: UUID = Depends(current_user_id)) -> dict:
    """Host override: reveal now instead of waiting for every answer."""
    async with pool(request).acquire() as db:
        room_id = await require_host(db, round_id, user_id)
    decision = await host_override(pool(request), room_id, round_id, Action.REVEAL)
    return {"action": decision.action.value}


@router.post("/rounds/{round_id}/advance")
async def advance_round(round_id: UUID, request: Request, user_id: UUID = Depends(current_user_id)) -> dict:
    """Host override: move to the next round instead of waiting for the chat to wind down."""
    async with pool(request).acquire() as db:
        room_id = await require_host(db, round_id, user_id)
    decision = await host_override(pool(request), room_id, round_id, Action.NEXT_ROUND)
    return {"action": decision.action.value}


@router.get("/rooms/{room_id}")
async def get_room(room_id: UUID, request: Request, user_id: UUID = Depends(current_user_id)) -> dict:
    async with pool(request).acquire() as db:
        await require_member(db, room_id, user_id)
        room = await db.fetchrow("SELECT id, name, join_code, host_profile_id, created_at FROM rooms WHERE id = $1", room_id)
        members = await room_members(db, room_id)
        session = await db.fetchrow(
            "SELECT id, vibe, status FROM game_sessions WHERE room_id = $1 ORDER BY created_at DESC LIMIT 1", room_id
        )
    return {**dict(room), "members": [{"profile_id": k, "display_name": v} for k, v in members.items()],
            "session": dict(session) if session else None}


@router.get("/rooms/{room_id}/timeline")
async def get_timeline(room_id: UUID, request: Request, user_id: UUID = Depends(current_user_id),
                       limit: int = 200) -> list:
    """Bootstrap after launch or reconnect. Live updates come from the Realtime subscription."""
    async with pool(request).acquire() as db:
        await require_member(db, room_id, user_id)
        rows = await db.fetch(
            """SELECT id, event_type, actor_profile_id, payload, created_at FROM timeline_events
               WHERE room_id = $1 ORDER BY created_at DESC LIMIT $2""",
            room_id, min(limit, 500),
        )
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in reversed(rows)]
