from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, status

from app.auth import current_user_id
from app.domain import (
    CreateRoomRequest,
    DemoSessionRequest,
    JoinRoomRequest,
    MaterialChoice,
    MessageRequest,
    SendGameRequest,
    StartSessionRequest,
    SubmitResponseRequest,
    ThreadRoomRequest,
)
from app.services import host
from app.services.game import GameService
from app.services.game_master import background
from app.services.material import my_material, set_excluded

router = APIRouter()


async def game_service(request: Request, user_id: Annotated[UUID, Depends(current_user_id)]):
    async with request.app.state.pool.acquire() as db:
        # Any request from a phone counts as it still being here (see IDLE in services/game.py).
        await db.execute(
            "UPDATE demo_identities SET last_seen_at=now() "
            "WHERE user_id=$1 AND last_seen_at < now() - interval '30 seconds'",
            user_id,
        )
        yield GameService(db)


UserId = Annotated[UUID, Depends(current_user_id)]
Service = Annotated[GameService, Depends(game_service)]


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/demo-sessions", status_code=status.HTTP_204_NO_CONTENT)
async def choose_demo_profile(
    *,
    payload: DemoSessionRequest,
    user_id: UserId,
    service: Service,
) -> None:
    await service.bind_identity(user_id, payload.profile_id)


@router.delete("/demo-sessions", status_code=status.HTTP_204_NO_CONTENT)
async def release_demo_profile(*, user_id: UserId, service: Service) -> None:
    await service.release_identity(user_id)


@router.post("/rooms", status_code=201)
async def create_room(
    *,
    payload: CreateRoomRequest,
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.create_room(user_id, payload.name)


@router.post("/rooms/join")
async def join_room(
    *,
    payload: JoinRoomRequest,
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.join(user_id, payload.code)


ThreadKey = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


@router.post("/threads/{thread_key}/room")
async def thread_room(
    *,
    thread_key: ThreadKey,
    payload: ThreadRoomRequest,
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.thread_room(user_id, thread_key, payload.name)


@router.post("/threads/{thread_key}/games", status_code=201)
async def send_game(
    *,
    thread_key: ThreadKey,
    payload: SendGameRequest,
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.send_game(user_id, thread_key, payload.name, payload.mode)


@router.post("/rooms/{room_id}/leave")
async def leave_room(
    *,
    room_id: UUID,
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.leave(user_id, room_id)


@router.get("/rooms/{room_id}")
async def hydrate_room(
    *,
    room_id: UUID,
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.hydrate(user_id, room_id)


@router.get("/rooms/{room_id}/timeline")
async def get_timeline(
    *,
    room_id: UUID,
    before: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.timeline(user_id, room_id, before, limit)


@router.post("/rooms/{room_id}/messages", status_code=201)
async def post_message(
    *,
    room_id: UUID,
    payload: MessageRequest,
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.message(user_id, room_id, payload.body)


@router.post("/rooms/{room_id}/sessions", status_code=201)
async def start_session(
    *,
    room_id: UUID,
    payload: StartSessionRequest,
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.start(user_id, room_id)


@router.post("/rounds/{round_id}/responses", status_code=201)
async def submit_response(
    *,
    round_id: UUID,
    payload: SubmitResponseRequest,
    user_id: UserId,
    service: Service,
    request: Request,
) -> dict:
    result = await service.submit(user_id, round_id, payload.value, payload.why)
    pool = getattr(request.app.state, "pool", None)
    # The host speaks after the answer is saved, in the background: the model never delays a player.
    if pool is not None and result.get("round", {}).get("phase") in ("revealed", "complete"):
        background(host.after_reveal(pool, round_id))
    if pool is not None and (session := result.get("session")):
        background(host.after_game(pool, session["id"], session["room_id"]))
    return result


@router.post("/rounds/{round_id}/reveal")
async def reveal_round(
    *,
    round_id: UUID,
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.reveal(user_id, round_id)


@router.post("/rounds/{round_id}/advance")
async def advance_round(
    *,
    round_id: UUID,
    user_id: UserId,
    service: Service,
) -> dict:
    return await service.advance(user_id, round_id)


@router.get("/rooms/{room_id}/gm-decisions")
async def game_master_decisions(
    *,
    room_id: UUID,
    user_id: UserId,
    service: Service,
) -> list:
    """Debug view of conductor decisions. Newest first. Membership required."""
    await service.room_access(room_id, user_id)
    rows = await service.db.fetch(
        """SELECT action, reason, p_silence, model_source, target_profile_id, features, created_at
           FROM gm_decisions WHERE room_id = $1 ORDER BY created_at DESC LIMIT 200""",
        room_id,
    )
    return [dict(row) for row in rows]


@router.get("/me/material")
async def get_my_material(*, user_id: UserId, service: Service) -> dict:
    """What the game may use from you: messages and photos you sent in shared chats, and your own
    activity. Anything marked excluded is never used."""
    return await my_material(service.db, await service.profile(user_id))


@router.put("/me/material/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def choose_material(
    *, item_id: UUID, payload: MaterialChoice, user_id: UserId, service: Service
) -> None:
    await set_excluded(service.db, await service.profile(user_id), item_id, payload.excluded)


@router.get("/threads/{thread_key}/suggestion")
async def game_suggestion(
    *, thread_key: ThreadKey, user_id: UserId, service: Service, request: Request
) -> dict:
    """An offer to play in a chat that's gone quiet. Only a suggestion: nothing starts on its own."""
    room = await service.db.fetchrow("SELECT id FROM rooms WHERE thread_key=$1", thread_key)
    if room is None:
        return {"suggest": False, "reason": "no room yet"}
    await service.room_access(room["id"], user_id)
    return await host.suggestion(request.app.state.pool, room["id"], await service.profile(user_id))
