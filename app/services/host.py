"""The AI host in async games. It speaks when something happens, not on a stopwatch:
- a round reveals: one line handing the turn to the person with the story;
- nobody picks it up: one follow-up, only if the timing model says the chat has gone quiet;
- the game ends: a closing line (recap);
- a chat has been quiet for days: a suggestion to play, which people can ignore.
All model calls run in the background, after the request that triggered them has finished."""

import json
import logging
import time
from datetime import UTC, datetime
from uuid import UUID

from app.ai.conductor import Conductor
from app.ai.conductor_features import Turn
from app.ai.guard import check_round_text, names_in
from app.ai.host import nudge_line
from app.ai.llm import complete_json
from app.ai.recap import FALLBACK, recap_line
from app.ai.rounds import VOICE
from app.services.rounds import decoded, room_members

log = logging.getLogger(__name__)

QUIET_DAYS = 3  # a chat this quiet gets a suggestion to play
SUGGESTION_TTL_S = 600
_suggestions: dict[str, tuple[float, dict]] = {}


async def _post(db, room_id, event_type: str, payload: dict) -> None:
    await db.execute(
        "INSERT INTO timeline_events(room_id,event_type,payload,created_at) VALUES($1,$2,$3,clock_timestamp())",
        room_id,
        event_type,
        payload,
    )


def _spotlight(game_type: str, reveal: dict) -> str | None:
    """Who has the story: whoever sent it (guessing rounds), or whose take won (open rounds)."""
    return reveal.get("correct_profile_id") or reveal.get("winner_profile_id")


async def _recent_chat(db, room_id, since=None) -> list[dict]:
    return [
        dict(r)
        for r in await db.fetch(
            """SELECT actor_profile_id, payload->>'body' AS body, created_at FROM timeline_events
               WHERE room_id=$1 AND event_type='message' AND ($2::timestamptz IS NULL OR created_at >= $2)
               ORDER BY created_at""",
            room_id,
            since,
        )
    ]


async def after_reveal(pool, round_id: UUID, follow_up: bool = False) -> None:
    """One line at the person with the story, the moment a round reveals (or once more, later)."""
    async with pool.acquire() as db:
        row = await db.fetchrow(
            "SELECT id, room_id, game_type, prompt, reveal, nudges FROM rounds WHERE id=$1",
            round_id,
        )
        reveal = decoded(row["reveal"]) or {}
        target = _spotlight(row["game_type"], reveal)
        if not target:
            return
        names = await room_members(db, row["room_id"])
        chat = await _recent_chat(db, row["room_id"])
    if target not in names:
        return
    mine = next((r for r in reveal.get("results", []) if r["profile_id"] == target), {})
    line, written_by = await nudge_line(
        names[target],
        list(names.values()),
        row["prompt"],
        reveal.get("message", ""),
        [c["body"] for c in chat[-6:] if c["body"]],
        game_type=row["game_type"],
        their_pick="open" if row["game_type"] == "hot_take" else None,
        votes={"their_take": mine.get("why")} if mine.get("why") else None,
        follow_up=follow_up,
    )
    async with pool.acquire() as db, db.transaction():
        await db.execute(
            "UPDATE rounds SET nudges = nudges + 1, last_nudge_at = now() WHERE id=$1", round_id
        )
        await _post(
            db,
            row["room_id"],
            "host_line",
            {
                "text": line,
                "target_profile_id": target,
                "round_id": str(round_id),
                "written_by": written_by,
                "kind": "follow_up" if follow_up else "reveal",
            },
        )


async def after_game(pool, session_id: UUID, room_id: UUID) -> None:
    """The closing line: what happened, and which round got people talking."""
    async with pool.acquire() as db:
        names = await room_members(db, room_id)
        rows = await db.fetch(
            """SELECT ordinal, game_type, prompt, reveal, revealed_at FROM rounds
               WHERE session_id=$1 AND revealed_at IS NOT NULL ORDER BY revealed_at""",
            session_id,
        )
        chat = await _recent_chat(db, room_id, rows[0]["revealed_at"] if rows else None)
    rounds = []
    for i, r in enumerate(rows):
        reveal = decoded(r["reveal"]) or {}
        until = rows[i + 1]["revealed_at"] if i + 1 < len(rows) else None
        who = _spotlight(r["game_type"], reveal)
        rounds.append(
            {
                "game_type": r["game_type"],
                "prompt": r["prompt"],
                "reveal": reveal.get("shoutout") or reveal.get("message"),
                "spotlight": names.get(str(who)) if who else None,
                "messages_after_reveal": sum(
                    1
                    for c in chat
                    if c["created_at"] >= r["revealed_at"]
                    and (until is None or c["created_at"] < until)
                ),
            }
        )
    line, written_by = (
        await recap_line(rounds, list(names.values())) if rounds else (FALLBACK, "template")
    )
    async with pool.acquire() as db:
        await _post(
            db,
            room_id,
            "game_recap",
            {
                "session_id": str(session_id),
                "text": line,
                "written_by": written_by,
                "rounds": rounds,
            },
        )


async def follow_ups(pool, conductor: Conductor) -> None:
    """Revealed async rounds whose person with the story hasn't answered the host: nudge once more,
    but only when the trained timing model says the chat has actually gone quiet."""
    pace = conductor.pace
    async with pool.acquire() as db:
        due = await db.fetch(
            """SELECT r.id, r.room_id, r.reveal, r.revealed_at, r.last_nudge_at, extract(epoch from now()) AS now
               FROM rounds r JOIN game_sessions s ON s.id = r.session_id
               WHERE s.mode = 'async' AND r.nudges = 1 AND r.last_nudge_at IS NOT NULL
                 AND r.revealed_at > now() - interval '2 hours'
                 AND r.last_nudge_at < now() - make_interval(secs => $1)""",
            float(pace.nudge_grace_s * 3),
        )
    for r in due:
        target = _spotlight("", decoded(r["reveal"]) or {})
        async with pool.acquire() as db:
            members = [
                str(m)
                for m in await db.fetch(
                    "SELECT profile_id FROM room_members WHERE room_id=$1 AND left_at IS NULL",
                    r["room_id"],
                )
            ]
            chat = await _recent_chat(db, r["room_id"], r["revealed_at"])
        if any(
            str(c["actor_profile_id"]) == target and c["created_at"] > r["last_nudge_at"]
            for c in chat
        ):
            continue  # they answered; the host's job is done
        now = float(r["now"])
        turns = [
            Turn(
                ts=c["created_at"].timestamp(),
                sender=str(c["actor_profile_id"]),
                length=len(c["body"] or ""),
                is_question="?" in (c["body"] or ""),
                has_media=False,
            )
            for c in chat
        ]
        p, _ = conductor.p_silence(turns, now, is_group=len(members) > 2)
        if p < pace.speak_threshold:
            continue  # people are still talking without the host
        try:
            await after_reveal(pool, r["id"], follow_up=True)
        except Exception:
            log.exception("follow-up nudge failed")


SUGGEST_SYSTEM = VOICE + (
    " This group chat has gone quiet. Write ONE short, friendly line offering (never pushing) a quick "
    "game, with one specific reason from what they share below. It's a suggestion they can ignore: "
    'phrase it as an offer, e.g. "it\'s been a minute since tybee. want to play a quick round?". '
    'Under 20 words. Never name anyone. JSON: {"line": "..."}'
)


async def suggestion(pool, room_id: UUID, viewer: UUID) -> dict:
    """Whether to offer a game in this chat, and the one-line offer. Never starts anything."""
    cached = _suggestions.get(str(room_id))
    if cached and time.time() - cached[0] < SUGGESTION_TTL_S:
        return cached[1]
    async with pool.acquire() as db:
        if await db.fetchval(
            "SELECT EXISTS(SELECT 1 FROM game_sessions WHERE room_id=$1 AND status='active')",
            room_id,
        ):
            return {"suggest": False, "reason": "a game is already running"}
        names = await room_members(db, room_id)
        members = list(names)
        last = await db.fetchval(
            """SELECT max(t) FROM (
                 SELECT max(created_at) t FROM timeline_events WHERE room_id=$1 AND event_type='message'
                 UNION ALL
                 SELECT max(occurred_at) FROM group_context_items
                 WHERE sender_profile_id = ANY($2::uuid[]) AND participant_profile_ids <@ $2::uuid[]
               ) x""",
            room_id,
            members,
        )
        shared = [
            r["label"]
            for r in await db.fetch(
                """SELECT label FROM moments WHERE retired_at IS NULL
                   AND participant_profile_ids <@ $1::uuid[] ORDER BY last_at DESC LIMIT 4""",
                members,
            )
        ]
    days = (datetime.now(UTC) - last).days if last else None
    if len(names) < 2 or (days is not None and days < QUIET_DAYS):
        return {"suggest": False, "reason": "chat is active"}
    quiet = f"{days} days" if days is not None else "a while"
    reply = await complete_json(
        SUGGEST_SYSTEM,
        json.dumps({"quiet_for": quiet, "things_they_share": shared}, ensure_ascii=False),
    )
    line = str((reply or {}).get("line", "")).strip()
    written_by = "muse"
    if not line or check_round_text(line, 160) or names_in(line, list(names.values())):
        line, written_by = (
            f"this chat's been quiet for {quiet}. want to play a quick round?",
            "template",
        )
    result = {
        "suggest": True,
        "line": line,
        "quiet_days": days,
        "written_by": written_by,
        "based_on": shared,
    }
    _suggestions[str(room_id)] = (time.time(), result)
    return result
