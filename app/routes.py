import asyncio
import secrets
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import current_user_id
from app.domain import CreateRoomRequest, DemoSessionRequest, JoinRoomRequest, MessageRequest
from app.services.game_master import tick_room

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
    request.app.state.background_ticks = getattr(request.app.state, "background_ticks", set())
    task = asyncio.create_task(tick_room(pool(request), request.app.state.conductor, room_id))
    request.app.state.background_ticks.add(task)
    task.add_done_callback(request.app.state.background_ticks.discard)
    return dict(event)


@router.get("/rooms/{room_id}/gm-decisions")
async def game_master_decisions(room_id: UUID, request: Request, user_id: UUID = Depends(current_user_id)) -> list:
    """Debug view: what the game master decided, and why. Newest first."""
    async with pool(request).acquire() as db:
        profile_id = await profile_for_user(db, user_id)
        member = await db.fetchval(
            "SELECT EXISTS(SELECT 1 FROM room_members WHERE room_id = $1 AND profile_id = $2)", room_id, profile_id
        )
        if not member:
            raise HTTPException(status_code=403, detail="Not a room member")
        rows = await db.fetch(
            """SELECT action, reason, p_silence, model_source, target_profile_id, features, created_at
               FROM gm_decisions WHERE room_id = $1 ORDER BY created_at DESC LIMIT 200""",
            room_id,
        )
        return [dict(r) for r in rows]
