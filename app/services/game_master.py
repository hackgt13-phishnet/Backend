"""Runs the conductor against live rooms and applies its decisions."""

import asyncio
import logging
from uuid import UUID

from app.ai.conductor import Action, Conductor, Decision, RoomState
from app.ai.conductor_features import Turn
from app.ai.host import nudge_line
from app.domain import RoundPhase
from app.services.game import GameService
from app.services.rounds import decoded, room_members

log = logging.getLogger(__name__)
CONTEXT_WINDOW = (
    "10 minutes"  # chat before the round opened that still counts as the same conversation
)


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
                  r.last_nudge_at, r.options, extract(epoch from now()) AS now
           FROM rounds r JOIN game_sessions s ON s.id = r.session_id
           LEFT JOIN round_answers a ON a.round_id = r.id
           WHERE s.room_id = $1 AND s.status = 'active' AND s.mode = 'live'
             AND r.ordinal = s.current_round_ordinal AND r.phase IN ('answering', 'revealed')
           ORDER BY r.ordinal DESC LIMIT 1""",
        room_id,
    )
    if not round_row:
        return None
    members = await db.fetch(
        "SELECT unnest(eligible_profile_ids) AS profile_id FROM private.round_secrets WHERE round_id=$1",
        round_row["id"],
    )
    responded = await db.fetch(
        "SELECT profile_id FROM round_responses WHERE round_id = $1", round_row["id"]
    )
    since = round_row["revealed_at"] or round_row["opened_at"]
    messages = await db.fetch(
        f"""SELECT extract(epoch from created_at) AS ts, actor_profile_id, payload->>'body' AS body
            FROM timeline_events
            WHERE room_id = $1 AND event_type = 'message'
              AND created_at >= coalesce($2::timestamptz, now()) - interval '{CONTEXT_WINDOW}'
            ORDER BY created_at""",
        room_id,
        since,
    )
    turns = tuple(
        Turn(
            ts=float(m["ts"]),
            sender=str(m["actor_profile_id"]),
            length=len(m["body"] or ""),
            is_question="?" in (m["body"] or ""),
            has_media=False,
        )
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
        story_holder_id=str(round_row["story_holder_profile_id"])
        if round_row["story_holder_profile_id"]
        else None,
    )
    return state, round_row["id"], round_row["session_id"]


async def apply(
    db, room_id: UUID, round_id: UUID, state: RoomState, decision: Decision
) -> str | None:
    """Apply the decision inside the room's lock. Returns follow-up work to run after commit."""
    await db.execute(
        """INSERT INTO gm_decisions(room_id, round_id, action, reason, p_silence, model_source,
                                    target_profile_id, features)
           VALUES($1, $2, $3, $4, $5, $6, $7, $8::jsonb)""",
        room_id,
        round_id,
        decision.action.value,
        decision.reason,
        decision.p_silence,
        decision.model_source,
        UUID(decision.target_id) if decision.target_id else None,
        decision.features,
    )
    service = GameService(db)
    if decision.action == Action.REVEAL:
        row = await db.fetchrow("SELECT * FROM rounds WHERE id=$1 FOR UPDATE", round_id)
        await service.reveal_current(row)
        return None
    if decision.action == Action.NUDGE:
        await db.execute(
            "UPDATE rounds SET nudges = nudges + 1, last_nudge_at = now() WHERE id = $1", round_id
        )
        return "nudge"
    if decision.action == Action.NEXT_ROUND:
        row = await db.fetchrow("SELECT * FROM rounds WHERE id=$1 FOR UPDATE", round_id)
        session = await db.fetchrow("SELECT * FROM game_sessions WHERE id=$1", row["session_id"])
        await service.advance_current(row, session)
    return None


async def post_nudge(pool, room_id: UUID, round_id: UUID, target_id: str) -> None:
    async with pool.acquire() as db:
        names = await room_members(db, room_id)
        rnd = await db.fetchrow(
            """SELECT r.game_type, r.prompt, a.reveal_copy
               FROM rounds r JOIN round_answers a ON a.round_id = r.id WHERE r.id = $1""",
            round_id,
        )
        picks = {
            str(r["profile_id"]): decoded(r["value"])
            for r in await db.fetch(
                "SELECT profile_id, value FROM round_responses WHERE round_id = $1", round_id
            )
        }
        recent = await db.fetch(
            """SELECT payload->>'body' AS body FROM timeline_events
               WHERE room_id = $1 AND event_type = 'message' ORDER BY created_at DESC LIMIT 6""",
            room_id,
        )
    votes = {names[pid]: pick for pid, pick in picks.items() if pid in names}
    line, written_by = await nudge_line(
        names[target_id],
        list(names.values()),
        rnd["prompt"],
        rnd["reveal_copy"],
        [r["body"] for r in reversed(recent)],
        game_type=rnd["game_type"],
        their_pick=picks.get(target_id),
        votes=votes,
    )
    async with pool.acquire() as db:
        await db.execute(
            """INSERT INTO timeline_events(room_id, event_type, payload)
               VALUES($1, 'host_line', jsonb_build_object('text', $2::text, 'target_profile_id', $3::text,
                                                         'round_id', $4::text, 'written_by', $5::text))""",
            room_id,
            line,
            target_id,
            str(round_id),
            written_by,
        )


async def tick_room(pool, conductor: Conductor, room_id: UUID) -> Decision | None:
    async with pool.acquire() as db, db.transaction():
        # One decision at a time per room, even if a message and the timer arrive together.
        if not await db.fetchval(
            "SELECT id FROM rooms WHERE id=$1 FOR UPDATE SKIP LOCKED", room_id
        ):
            return None
        loaded = await load_state(db, room_id)
        if loaded is None:
            return None
        state, round_id, _session_id = loaded
        decision = conductor.decide(state)
        follow_up = await apply(db, room_id, round_id, state, decision)
    # LLM calls run after the lock is released, in the background, so a slow model never blocks a room.
    if follow_up == "nudge" and decision.target_id:
        background(post_nudge(pool, room_id, round_id, decision.target_id))
    return decision


async def expire_async_rounds(pool, conductor: Conductor) -> None:
    """Reveal async rounds whose demo/normal wait has run out. Live rooms are not touched."""
    timeout = conductor.pace.async_answer_timeout_s
    if not timeout:
        return
    async with pool.acquire() as db:
        rows = await db.fetch(
            """SELECT r.id
               FROM rounds r
               JOIN game_sessions s ON s.id = r.session_id
               JOIN private.round_secrets sec ON sec.round_id = r.id
               WHERE s.status = 'active' AND s.mode = 'async' AND r.phase = 'answering'
                 AND (
                   SELECT max(rr.submitted_at) FROM round_responses rr WHERE rr.round_id = r.id
                 ) <= now() - make_interval(secs => $1)
                 AND cardinality(r.submitted_profile_ids) > 0
                 AND cardinality(r.submitted_profile_ids) < cardinality(sec.eligible_profile_ids)""",
            float(timeout),
        )
        pending = [row["id"] for row in rows]
    for round_id in pending:
        try:
            async with pool.acquire() as db:
                await GameService(db).expire_waiting(round_id)
        except Exception:
            log.exception("async wait expire failed for %s", round_id)


async def run_loop(pool, conductor: Conductor) -> None:
    while True:
        try:
            async with pool.acquire() as db:
                rooms = await db.fetch(
                    # Async games settle on the last answer; a separate clock covers stragglers.
                    "SELECT DISTINCT room_id FROM game_sessions WHERE status = 'active' AND mode = 'live'"
                )
            for row in rooms:
                await tick_room(pool, conductor, row["room_id"])
            await expire_async_rounds(pool, conductor)
        except Exception:
            log.exception("game master tick failed")
        await asyncio.sleep(conductor.pace.tick_s)
