"""Trusted PostgreSQL commands. All room writes lock the room before game rows."""

import base64
import json
import secrets
from datetime import datetime
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from app.domain import PublicRound, RoundPhase
from app.services.round_fixture import build_rounds

ALLOWED_TRANSITIONS: dict[RoundPhase, set[RoundPhase]] = {
    RoundPhase.PENDING: {RoundPhase.ANSWERING},
    RoundPhase.ANSWERING: {RoundPhase.REVEALED},
    RoundPhase.REVEALED: {RoundPhase.COMPLETE},
    RoundPhase.COMPLETE: set(),
}


def can_transition(current: RoundPhase, target: RoundPhase) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: RoundPhase, target: RoundPhase) -> None:
    if not can_transition(current, target):
        raise ValueError(f"Cannot transition round from {current} to {target}")


def require_transition(current: str, target: RoundPhase) -> None:
    try:
        assert_transition(RoundPhase(current), target)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


def public_round(row) -> dict:
    # Explicit allowlist, including nested reveal validation. Never serialize secrets.
    return PublicRound.model_validate(dict(row)).model_dump(mode="json")


def check_submission(round_row, secret, actor: UUID, value: UUID) -> None:
    if round_row["phase"] != "answering":
        raise HTTPException(409, "Round is not answering")
    if actor not in secret["eligible_profile_ids"]:
        raise HTTPException(403, "Not an eligible respondent")
    if actor in round_row["submitted_profile_ids"]:
        raise HTTPException(409, "Response already submitted")
    if str(value) not in {str(option["profile_id"]) for option in round_row["options"]}:
        raise HTTPException(422, "Answer must be a round option")


def reveal_result(secret, responses) -> dict:
    eligible = set(secret["eligible_profile_ids"])
    if {r["profile_id"] for r in responses} != eligible:
        raise HTTPException(409, "Waiting for all eligible respondents")
    answer = secret["answer"]
    try:
        correct = str(UUID(answer["correct_profile_id"] if isinstance(answer, dict) else answer))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise HTTPException(
            409, "Legacy round answer needs a trusted profile-UUID mapping"
        ) from error
    return {
        "correct_profile_id": correct,
        "message": secret["reveal_copy"],
        "results": [
            {
                "profile_id": str(r["profile_id"]),
                "correct": str(r["value"]) == correct,
                "points": int(str(r["value"]) == correct),
            }
            for r in responses
        ],
    }


def encode_cursor(row) -> str:
    return base64.urlsafe_b64encode(
        json.dumps([row["created_at"].isoformat(), str(row["id"])]).encode()
    ).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        stamp, event_id = json.loads(base64.urlsafe_b64decode(cursor))
        when = datetime.fromisoformat(stamp)
        if when.tzinfo is None:
            raise ValueError("Timezone required")
        return when, UUID(event_id)
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise HTTPException(422, "Invalid timeline cursor") from error


class GameService:
    def __init__(self, db):
        self.db = db

    async def profile(self, user_id: UUID) -> UUID:
        profile = await self.db.fetchval(
            "SELECT profile_id FROM demo_identities WHERE user_id = $1", user_id
        )
        if profile is None:
            raise HTTPException(403, "Choose a demo profile first")
        return profile

    async def bind_identity(self, user_id: UUID, profile_id: UUID) -> None:
        async with self.db.transaction():
            if not await self.db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM profiles WHERE id=$1)", profile_id
            ):
                raise HTTPException(404, "Demo profile not found")
            await self.db.execute(
                "INSERT INTO demo_identities(user_id, profile_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                user_id,
                profile_id,
            )
            bound = await self.db.fetchval(
                "SELECT profile_id FROM demo_identities WHERE user_id=$1", user_id
            )
            if bound != profile_id:
                raise HTTPException(409, "Identity is already bound or profile is already claimed")

    async def room_access(self, room_id: UUID, user_id: UUID, *, host=False, lock=False):
        actor = await self.profile(user_id)
        query = "SELECT * FROM rooms WHERE id=$1" + (" FOR UPDATE" if lock else "")
        room = await self.db.fetchrow(query, room_id)
        if room is None:
            raise HTTPException(404, "Room not found")
        member = await self.db.fetchval(
            "SELECT EXISTS(SELECT 1 FROM room_members WHERE room_id=$1 AND profile_id=$2 "
            "AND left_at IS NULL)",
            room_id,
            actor,
        )
        if not member:
            raise HTTPException(403, "Not an active room member")
        if host and room["host_profile_id"] != actor:
            raise HTTPException(403, "Not the room host")
        return actor, room

    async def round_access(self, round_id: UUID, user_id: UUID, *, host=False):
        room_id = await self.db.fetchval("SELECT room_id FROM rounds WHERE id=$1", round_id)
        if room_id is None:
            raise HTTPException(404, "Round not found")
        actor, _ = await self.room_access(room_id, user_id, host=host, lock=True)
        row = await self.db.fetchrow("SELECT * FROM rounds WHERE id=$1 FOR UPDATE", round_id)
        session = await self.db.fetchrow(
            "SELECT * FROM game_sessions WHERE id=$1", row["session_id"]
        )
        if session["status"] != "active" or session["current_round_ordinal"] != row["ordinal"]:
            raise HTTPException(409, "Not the current active round")
        return actor, row, session

    async def event(self, room_id, event_type, payload, actor=None):
        row = await self.db.fetchrow(
            "INSERT INTO timeline_events(room_id,event_type,actor_profile_id,payload) "
            "VALUES($1,$2,$3,$4) RETURNING *",
            room_id,
            event_type,
            actor,
            payload,
        )
        return dict(row)

    async def create_room(self, user_id, name):
        actor = await self.profile(user_id)
        for _ in range(5):
            try:
                async with self.db.transaction():
                    room = await self.db.fetchrow(
                        "INSERT INTO rooms(name,join_code,host_profile_id) VALUES($1,$2,$3) RETURNING *",
                        name,
                        secrets.token_hex(4).upper(),
                        actor,
                    )
                    await self.db.execute(
                        "INSERT INTO room_members(room_id,profile_id,role) VALUES($1,$2,'host')",
                        room["id"],
                        actor,
                    )
                    return dict(room)
            except asyncpg.UniqueViolationError:
                continue
        raise HTTPException(503, "Could not allocate a room code")

    async def join(self, user_id, code):
        async with self.db.transaction():
            actor = await self.profile(user_id)
            room = await self.db.fetchrow(
                "SELECT * FROM rooms WHERE join_code=$1 FOR UPDATE", code.upper()
            )
            if room is None:
                raise HTTPException(404, "Room code not found")
            await self.db.execute(
                "INSERT INTO room_members(room_id,profile_id) VALUES($1,$2) "
                "ON CONFLICT(room_id,profile_id) DO UPDATE SET left_at=NULL "
                "WHERE room_members.left_at IS NOT NULL",
                room["id"],
                actor,
            )
            return dict(room)

    async def leave(self, user_id, room_id):
        async with self.db.transaction():
            actor, room = await self.room_access(room_id, user_id, lock=True)
            if actor == room["host_profile_id"]:
                raise HTTPException(409, "Host departure is not supported in this demo")
            if await self.db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM game_sessions WHERE room_id=$1 AND status='active')",
                room_id,
            ):
                raise HTTPException(409, "Finish the active session before leaving")
            row = await self.db.fetchrow(
                "UPDATE room_members SET left_at=now() WHERE room_id=$1 AND profile_id=$2 RETURNING *",
                room_id,
                actor,
            )
            return dict(row)

    async def start(self, user_id, room_id):
        async with self.db.transaction():
            await self.room_access(room_id, user_id, host=True, lock=True)
            if await self.db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM game_sessions WHERE room_id=$1 AND status='active')",
                room_id,
            ):
                raise HTTPException(409, "A session is already active")
            players = await self.db.fetch(
                "SELECT p.id,p.display_name FROM profiles p JOIN room_members m ON m.profile_id=p.id "
                "WHERE m.room_id=$1 AND m.left_at IS NULL ORDER BY p.id",
                room_id,
            )
            if not 2 <= len(players) <= 6:
                raise HTTPException(409, "Demo requires 2–6 active players")
            drafts = build_rounds(players)
            session = await self.db.fetchrow(
                "INSERT INTO game_sessions(room_id,vibe) VALUES($1,'chaos') RETURNING *", room_id
            )
            first = None
            for ordinal, draft in enumerate(drafts, 1):
                row = await self.db.fetchrow(
                    "INSERT INTO rounds(session_id,room_id,ordinal,game_type,phase,prompt,options,media,"
                    "required_response_count) VALUES($1,$2,$3,'who_sent_this',$4,$5,$6,$7,$8) RETURNING *",
                    session["id"],
                    room_id,
                    ordinal,
                    "answering" if ordinal == 1 else "pending",
                    draft["prompt"],
                    draft["options"],
                    draft["media"],
                    len(players),
                )
                await self.db.execute(
                    "INSERT INTO private.round_secrets(round_id,answer,reveal_copy,eligible_profile_ids) "
                    "VALUES($1,$2,$3,$4)",
                    row["id"],
                    draft["answer"],
                    draft["reveal_copy"],
                    [p["id"] for p in players],
                )
                if ordinal == 1:
                    first = public_round(row)
            await self.event(room_id, "game_started", {"session_id": str(session["id"])})
            await self.event(room_id, "game_prompt", first)
            return {"session": dict(session), "current_round": first}

    async def submit(self, user_id, round_id, value):
        async with self.db.transaction():
            actor, row, _ = await self.round_access(round_id, user_id)
            secret = await self.db.fetchrow(
                "SELECT * FROM private.round_secrets WHERE round_id=$1", round_id
            )
            check_submission(row, secret, actor, value)
            await self.db.execute(
                "INSERT INTO round_responses(round_id,profile_id,value) VALUES($1,$2,$3)",
                round_id,
                actor,
                str(value),
            )
            row = await self.db.fetchrow(
                "UPDATE rounds SET submitted_profile_ids=array_append(submitted_profile_ids,$2::uuid) "
                "WHERE id=$1 RETURNING *",
                round_id,
                actor,
            )
            status = {
                "round_id": str(round_id),
                "submitted_profile_ids": [str(p) for p in row["submitted_profile_ids"]],
                "required_response_count": row["required_response_count"],
                "revision": row["revision"],
            }
            await self.event(row["room_id"], "submission_status", status)
            return {"accepted": True, **status}

    async def reveal(self, user_id, round_id):
        async with self.db.transaction():
            _, row, _ = await self.round_access(round_id, user_id, host=True)
            require_transition(row["phase"], RoundPhase.REVEALED)
            secret = await self.db.fetchrow(
                "SELECT * FROM private.round_secrets WHERE round_id=$1", round_id
            )
            responses = await self.db.fetch(
                "SELECT profile_id,value FROM round_responses WHERE round_id=$1 ORDER BY profile_id",
                round_id,
            )
            result = reveal_result(secret, responses)
            updated = await self.db.fetchrow(
                "UPDATE rounds SET phase='revealed',reveal=$2 WHERE id=$1 RETURNING *",
                round_id,
                result,
            )
            public = public_round(updated)
            await self.event(row["room_id"], "game_reveal", public)
            return public

    async def advance(self, user_id, round_id):
        async with self.db.transaction():
            _, row, session = await self.round_access(round_id, user_id, host=True)
            require_transition(row["phase"], RoundPhase.COMPLETE)
            completed = await self.db.fetchrow(
                "UPDATE rounds SET phase='complete' WHERE id=$1 RETURNING *", round_id
            )
            next_round = await self.db.fetchrow(
                "SELECT * FROM rounds WHERE session_id=$1 AND ordinal=$2",
                session["id"],
                row["ordinal"] + 1,
            )
            if next_round:
                require_transition(next_round["phase"], RoundPhase.ANSWERING)
                current = await self.db.fetchrow(
                    "UPDATE rounds SET phase='answering' WHERE id=$1 RETURNING *", next_round["id"]
                )
                session = await self.db.fetchrow(
                    "UPDATE game_sessions SET current_round_ordinal=$2 WHERE id=$1 RETURNING *",
                    session["id"],
                    next_round["ordinal"],
                )
                await self.event(row["room_id"], "game_prompt", public_round(current))
            else:
                current = completed
                session = await self.db.fetchrow(
                    "UPDATE game_sessions SET status='complete' WHERE id=$1 RETURNING *",
                    session["id"],
                )
            return {"session": dict(session), "current_round": public_round(current)}

    async def message(self, user_id, room_id, body):
        async with self.db.transaction():
            actor, _ = await self.room_access(room_id, user_id, lock=True)
            return await self.event(room_id, "message", {"body": body}, actor)

    async def timeline_page(self, room_id, before=None, limit=50):
        stamp, event_id = decode_cursor(before) if before else (None, None)
        rows = await self.db.fetch(
            "SELECT * FROM timeline_events WHERE room_id=$1 AND "
            "($2::timestamptz IS NULL OR (created_at,id)<($2,$3::uuid)) "
            "ORDER BY created_at DESC,id DESC LIMIT $4",
            room_id,
            stamp,
            event_id,
            limit + 1,
        )
        page = rows[:limit]
        return {
            "events": [dict(r) for r in reversed(page)],
            "next_cursor": encode_cursor(page[-1]) if len(rows) > limit else None,
        }

    async def timeline(self, user_id, room_id, before=None, limit=50):
        async with self.db.transaction(isolation="repeatable_read", readonly=True):
            await self.room_access(room_id, user_id)
            return await self.timeline_page(room_id, before, limit)

    async def hydrate(self, user_id, room_id):
        async with self.db.transaction(isolation="repeatable_read", readonly=True):
            actor, room = await self.room_access(room_id, user_id)
            members = await self.db.fetch(
                "SELECT m.*,p.display_name,p.avatar_url FROM room_members m "
                "JOIN profiles p ON p.id=m.profile_id WHERE m.room_id=$1 ORDER BY m.joined_at,m.profile_id",
                room_id,
            )
            session = await self.db.fetchrow(
                "SELECT * FROM game_sessions WHERE room_id=$1 "
                "ORDER BY (status='active') DESC,created_at DESC,id DESC LIMIT 1",
                room_id,
            )
            rounds = []
            current = None
            if session:
                rounds = [
                    public_round(r)
                    for r in await self.db.fetch(
                        "SELECT * FROM rounds WHERE session_id=$1 AND phase<>'pending' ORDER BY ordinal",
                        session["id"],
                    )
                ]
                current = next(
                    (r for r in rounds if r["ordinal"] == session["current_round_ordinal"]), None
                )
            history = await self.timeline_page(room_id)
            return {
                "viewer_profile_id": actor,
                "room": dict(room),
                "members": [dict(m) for m in members],
                "active_session": dict(session)
                if session and session["status"] == "active"
                else None,
                "last_session": dict(session)
                if session and session["status"] == "complete"
                else None,
                "rounds": rounds,
                "current_round": current,
                "viewer_has_submitted": bool(
                    current and str(actor) in current["submitted_profile_ids"]
                ),
                "timeline": history["events"],
                "timeline_cursor": history["next_cursor"],
            }
