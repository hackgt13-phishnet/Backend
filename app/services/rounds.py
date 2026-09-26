"""Creating, opening and revealing AI-written rounds."""

import asyncio
import json
from collections import Counter
from uuid import UUID

import numpy as np

from app.ai.picker import ItemView, MomentView, member_vectors_from_items, pick_moment
from app.ai.rounds import game_for, moment_preference, write_round
from app.domain import ROUNDS_PER_SESSION, RoundDraft


def _vector(text: str | None) -> np.ndarray | None:
    return np.array(json.loads(text), dtype=float) if text else None


async def room_members(db, room_id: UUID) -> dict[str, str]:
    rows = await db.fetch(
        """SELECT p.id, p.display_name FROM room_members m JOIN profiles p ON p.id = m.profile_id
           WHERE m.room_id = $1""",
        room_id,
    )
    return {str(r["id"]): r["display_name"] for r in rows}


async def load_context(pool, room_id: UUID, session_id: UUID):
    """Names, moments, items and member interest vectors for a room. No DB lock is held afterwards."""
    async with pool.acquire() as db:
        names = await room_members(db, room_id)
        used = await db.fetch("SELECT moment_id FROM rounds WHERE session_id = $1 AND moment_id IS NOT NULL", session_id)
        moment_rows = await db.fetch(
            "SELECT id, kind, item_ids, participant_profile_ids, centroid::text AS centroid FROM moments"
        )
        item_rows = await db.fetch(
            """SELECT id, sender_profile_id, participant_profile_ids, body, embedding::text AS embedding
               FROM group_context_items WHERE safe_for_demo AND body IS NOT NULL AND sender_profile_id IS NOT NULL"""
        )
    items = {
        str(r["id"]): ItemView(str(r["id"]), str(r["sender_profile_id"]),
                               frozenset(str(p) for p in r["participant_profile_ids"]), r["body"])
        for r in item_rows
    }
    item_vectors = {str(r["id"]): v for r in item_rows if (v := _vector(r["embedding"])) is not None}
    moments = [
        MomentView(str(r["id"]), r["kind"], tuple(str(i) for i in r["item_ids"]),
                   frozenset(str(p) for p in r["participant_profile_ids"]), _vector(r["centroid"]))
        for r in moment_rows
    ]
    used_ids = {str(r["moment_id"]) for r in used}
    return names, items, moments, member_vectors_from_items(item_vectors, items), used_ids


async def draft_rounds(pool, room_id: UUID, session_id: UUID, ordinals: list[int]) -> list[tuple[int, RoundDraft]]:
    """Pick a different moment for each round (fast, no LLM), then have Muse write them all at once."""
    names, items, moments, vectors, used = await load_context(pool, room_id, session_id)
    picks = []
    for ordinal in ordinals:
        pick = pick_moment(moments, items, frozenset(names), vectors, frozenset(used),
                           want=moment_preference(game_for(ordinal)))
        if pick is None:
            break
        used.add(pick.moment.id)
        picks.append((ordinal, pick))
    drafts = await asyncio.gather(*(write_round(o, p, names) for o, p in picks))
    return [(o, d) for (o, _), d in zip(picks, drafts, strict=True)]


async def draft_round(pool, room_id: UUID, session_id: UUID, ordinal: int) -> RoundDraft | None:
    drafted = await draft_rounds(pool, room_id, session_id, [ordinal])
    return drafted[0][1] if drafted else None


async def insert_round(db, session_id: UUID, ordinal: int, draft: RoundDraft, phase: str) -> UUID:
    round_id = await db.fetchval(
        """INSERT INTO rounds(session_id, ordinal, game_type, phase, prompt, options, reveal_copy,
                              moment_id, story_holder_profile_id, opened_at)
           VALUES($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, CASE WHEN $4 = 'answering' THEN now() END)
           ON CONFLICT (session_id, ordinal) DO NOTHING
           RETURNING id""",
        session_id, ordinal, draft.game_type.value, phase, draft.prompt,
        json.dumps({"choices": draft.options, "quote": draft.quote}), draft.reveal_copy,
        draft.moment_id, draft.story_holder_id,
    )
    if round_id is None:
        return await db.fetchval("SELECT id FROM rounds WHERE session_id = $1 AND ordinal = $2", session_id, ordinal)
    await db.execute(
        "INSERT INTO round_answers(round_id, answer, source_item_ids) VALUES($1, $2::jsonb, $3::uuid[])",
        round_id, json.dumps(draft.answer), draft.source_item_ids,
    )
    return round_id


async def announce_round(db, room_id: UUID, round_id: UUID) -> None:
    row = await db.fetchrow("SELECT ordinal, game_type, prompt, options FROM rounds WHERE id = $1", round_id)
    options = json.loads(row["options"])
    await db.execute(
        """INSERT INTO timeline_events(room_id, event_type, payload)
           VALUES($1, 'game_prompt', $2::jsonb)""",
        room_id, json.dumps({
            "round_id": str(round_id), "ordinal": row["ordinal"], "game_type": row["game_type"],
            "prompt": row["prompt"], "quote": options.get("quote"), "options": options["choices"],
        }),
    )


async def prefetch_next(pool, room_id: UUID, session_id: UUID) -> None:
    """Write the next round during the current discussion, so there's no wait when it opens."""
    async with pool.acquire() as db:
        next_ordinal = await db.fetchval(
            "SELECT coalesce(max(ordinal), 0) + 1 FROM rounds WHERE session_id = $1", session_id
        )
    if next_ordinal > ROUNDS_PER_SESSION:
        return
    draft = await draft_round(pool, room_id, session_id, next_ordinal)
    if draft is None:
        return
    async with pool.acquire() as db:
        await insert_round(db, session_id, next_ordinal, draft, "pending")


async def open_next(pool, room_id: UUID, session_id: UUID) -> bool:
    """Open the next pending round, writing it now if the prefetch hasn't finished. False = game over."""
    async with pool.acquire() as db:
        pending = await db.fetchval(
            "SELECT id FROM rounds WHERE session_id = $1 AND phase = 'pending' ORDER BY ordinal LIMIT 1", session_id
        )
    if pending is None:
        await prefetch_next(pool, room_id, session_id)
        async with pool.acquire() as db:
            pending = await db.fetchval(
                "SELECT id FROM rounds WHERE session_id = $1 AND phase = 'pending' ORDER BY ordinal LIMIT 1", session_id
            )
    async with pool.acquire() as db, db.transaction():
        if pending is None:
            await db.execute("UPDATE game_sessions SET status = 'complete' WHERE id = $1", session_id)
            await db.execute(
                """INSERT INTO timeline_events(room_id, event_type, payload)
                   VALUES($1, 'game_reveal', jsonb_build_object('session_id', $2::text, 'game_over', true))""",
                room_id, str(session_id),
            )
            return False
        await db.execute("UPDATE rounds SET phase = 'answering', opened_at = now() WHERE id = $1", pending)
        await announce_round(db, room_id, pending)
        return True


async def reveal_payload(db, round_id: UUID) -> tuple[dict, UUID | None]:
    """Everything clients need at the reveal, plus who should get the spotlight afterwards."""
    row = await db.fetchrow(
        """SELECT r.game_type, r.reveal_copy, r.story_holder_profile_id, a.answer
           FROM rounds r JOIN round_answers a ON a.round_id = r.id WHERE r.id = $1""",
        round_id,
    )
    responses = await db.fetch(
        """SELECT rr.profile_id, p.display_name, rr.value FROM round_responses rr
           JOIN profiles p ON p.id = rr.profile_id WHERE rr.round_id = $1""",
        round_id,
    )
    votes = Counter(json.loads(r["value"]) for r in responses)
    story_holder = row["story_holder_profile_id"]
    if story_holder is None and votes:
        top_name = votes.most_common(1)[0][0]
        story_holder = await db.fetchval("SELECT id FROM profiles WHERE display_name = $1", top_name)
    return {
        "round_id": str(round_id),
        "game_type": row["game_type"],
        "answer": json.loads(row["answer"]) if row["answer"] else None,
        "reveal": row["reveal_copy"],
        "responses": [{"profile_id": str(r["profile_id"]), "name": r["display_name"],
                       "value": json.loads(r["value"])} for r in responses],
        "votes": dict(votes),
    }, story_holder
