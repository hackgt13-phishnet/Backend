"""Runs the conductor against live rooms and applies its decisions."""

import asyncio
import json
import logging
from uuid import UUID

from fastapi import HTTPException

from app.ai.conductor import Action, Conductor, Decision, RoomState
from app.ai.conductor_features import Turn
from app.ai.host import nudge_line
from app.domain import RoundPhase
from app.services.game import assert_transition
from app.services.rounds import open_next, prefetch_next, reveal_payload, room_members

log = logging.getLogger(__name__)
TICK_S = 5
CONTEXT_WINDOW = "10 minutes"  # chat before the round opened that still counts as the same conversation


_tasks: set[asyncio.Task] = set()


def background(coro) -> asyncio.Task:
    """Fire-and-forget with a strong reference, and errors logged instead of swallowed."""
    task = asyncio.create_task(coro)
    _tasks.add(task)

    def done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.error("game master background task failed", exc_info=t.exception())

    task.add_done_callback(done)
    return task


def _epoch(value) -> float | None:
    return value.timestamp() if value else None


async def load_state(db, room_id: UUID) -> tuple[RoomState, UUID, UUID] | None:
    round_row = await db.fetchrow(
        """SELECT r.id, r.session_id, r.phase, r.opened_at, r.revealed_at, a.story_holder_profile_id, r.nudges,
                  r.last_nudge_at, extract(epoch from now()) AS now
           FROM rounds r JOIN game_sessions s ON s.id = r.session_id
           LEFT JOIN round_answers a ON a.round_id = r.id
           WHERE s.room_id = $1 AND s.status = 'active' AND r.phase IN ('answering', 'revealed')
           ORDER BY r.ordinal DESC LIMIT 1""",
        room_id,
    )
    if not round_row:
        return None
    members = await db.fetch("SELECT profile_id FROM room_members WHERE room_id = $1", room_id)
    responded = await db.fetch("SELECT profile_id FROM round_responses WHERE round_id = $1", round_row["id"])
    since = round_row["revealed_at"] or round_row["opened_at"]
    messages = await db.fetch(
        f"""SELECT extract(epoch from created_at) AS ts, actor_profile_id, payload->>'body' AS body
            FROM timeline_events
            WHERE room_id = $1 AND event_type = 'message'
              AND created_at >= coalesce($2::timestamptz, now()) - interval '{CONTEXT_WINDOW}'
            ORDER BY created_at""",
        room_id, since,
    )
    turns = tuple(
        Turn(ts=float(m["ts"]), sender=str(m["actor_profile_id"]), length=len(m["body"] or ""),
             is_question="?" in (m["body"] or ""), has_media=False)
        for m in messages
    )
    state = RoomState(
        phase=RoundPhase(round_row["phase"]),
        now=float(round_row["now"]),
        member_ids=frozenset(str(m["profile_id"]) for m in members),
        responded_ids=frozenset(str(r["profile_id"]) for r in responded),
        round_opened_at=_epoch(round_row["opened_at"]) or float(round_row["now"]),
        revealed_at=_epoch(round_row["revealed_at"]),
        turns=turns,
        nudges_this_round=round_row["nudges"],
        last_nudge_at=_epoch(round_row["last_nudge_at"]),
        story_holder_id=str(round_row["story_holder_profile_id"]) if round_row["story_holder_profile_id"] else None,
    )
    return state, round_row["id"], round_row["session_id"]


async def apply(db, room_id: UUID, round_id: UUID, state: RoomState, decision: Decision) -> str | None:
    """Apply the decision inside the room's lock. Returns follow-up work to run after commit."""
    await db.execute(
        """INSERT INTO gm_decisions(room_id, round_id, action, reason, p_silence, model_source,
                                    target_profile_id, features)
           VALUES($1, $2, $3, $4, $5, $6, $7, $8::jsonb)""",
        room_id, round_id, decision.action.value, decision.reason, decision.p_silence,
        decision.model_source, UUID(decision.target_id) if decision.target_id else None,
        json.dumps(decision.features),
    )
    if decision.action == Action.REVEAL:
        assert_transition(state.phase, RoundPhase.REVEALED)
        payload, story_holder = await reveal_payload(db, round_id)
        await db.execute("UPDATE rounds SET phase = 'revealed', revealed_at = now() WHERE id = $1", round_id)
        await db.execute(
            """UPDATE round_answers SET story_holder_profile_id = coalesce(story_holder_profile_id, $2)
               WHERE round_id = $1""",
            round_id, story_holder,
        )
        await db.execute(
            "INSERT INTO timeline_events(room_id, event_type, payload) VALUES($1, 'game_reveal', $2::jsonb)",
            room_id, json.dumps(payload),
        )
        return "prefetch"
    if decision.action == Action.NUDGE:
        await db.execute("UPDATE rounds SET nudges = nudges + 1, last_nudge_at = now() WHERE id = $1", round_id)
        return "nudge"
    if decision.action == Action.NEXT_ROUND:
        assert_transition(state.phase, RoundPhase.COMPLETE)
        await db.execute("UPDATE rounds SET phase = 'complete' WHERE id = $1", round_id)
        return "next"
    return None


async def post_nudge(pool, room_id: UUID, round_id: UUID, target_id: str) -> None:
    async with pool.acquire() as db:
        names = await room_members(db, room_id)
        rnd = await db.fetchrow("SELECT prompt, reveal_copy FROM rounds WHERE id = $1", round_id)
        recent = await db.fetch(
            """SELECT payload->>'body' AS body FROM timeline_events
               WHERE room_id = $1 AND event_type = 'message' ORDER BY created_at DESC LIMIT 6""",
            room_id,
        )
    line, written_by = await nudge_line(names[target_id], list(names.values()), rnd["prompt"],
                                        rnd["reveal_copy"], [r["body"] for r in reversed(recent)])
    async with pool.acquire() as db:
        await db.execute(
            """INSERT INTO timeline_events(room_id, event_type, payload)
               VALUES($1, 'host_line', jsonb_build_object('text', $2::text, 'target_profile_id', $3::text,
                                                         'round_id', $4::text, 'written_by', $5::text))""",
            room_id, line, target_id, str(round_id), written_by,
        )


async def tick_room(pool, conductor: Conductor, room_id: UUID) -> Decision | None:
    async with pool.acquire() as db, db.transaction():
        # One decision at a time per room, even if a message and the timer arrive together.
        if not await db.fetchval("SELECT pg_try_advisory_xact_lock(hashtext($1::text))", str(room_id)):
            return None
        loaded = await load_state(db, room_id)
        if loaded is None:
            return None
        state, round_id, session_id = loaded
        decision = conductor.decide(state)
        follow_up = await apply(db, room_id, round_id, state, decision)
    # LLM calls run after the lock is released, in the background, so a slow model never blocks a room.
    if follow_up == "prefetch":
        background(prefetch_next(pool, room_id, session_id))
    elif follow_up == "nudge" and decision.target_id:
        background(post_nudge(pool, room_id, round_id, decision.target_id))
    elif follow_up == "next":
        background(open_next(pool, room_id, session_id))
    return decision


async def host_override(pool, room_id: UUID, round_id: UUID, action: Action) -> Decision:
    """The host forces a reveal or advance. Logged like any other decision, with source "host"."""
    required = {Action.REVEAL: RoundPhase.ANSWERING, Action.NEXT_ROUND: RoundPhase.REVEALED}[action]
    async with pool.acquire() as db, db.transaction():
        await db.execute("SELECT pg_advisory_xact_lock(hashtext($1::text))", str(room_id))
        loaded = await load_state(db, room_id)
        if loaded is None or loaded[1] != round_id or loaded[0].phase != required:
            raise HTTPException(status_code=409, detail=f"That round isn't in the {required.value} phase")
        state, _, session_id = loaded
        decision = Decision(action, "host override", model_source="host")
        follow_up = await apply(db, room_id, round_id, state, decision)
    if follow_up == "prefetch":
        background(prefetch_next(pool, room_id, session_id))
    elif follow_up == "next":
        background(open_next(pool, room_id, session_id))
    return decision


async def run_loop(pool, conductor: Conductor) -> None:
    while True:
        try:
            async with pool.acquire() as db:
                rooms = await db.fetch("SELECT DISTINCT room_id FROM game_sessions WHERE status = 'active'")
            for row in rooms:
                await tick_room(pool, conductor, row["room_id"])
        except Exception:
            log.exception("game master tick failed")
        await asyncio.sleep(TICK_S)
