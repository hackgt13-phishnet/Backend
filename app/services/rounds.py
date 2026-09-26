"""Creating, opening and revealing AI-written rounds."""

import asyncio
import json
from collections import Counter
from uuid import UUID

import numpy as np

from app.ai.interests import ActivityItem, Interest, extract_interests, find_links
from app.ai.picker import ItemView, MomentView, member_vectors_from_items, score_moments
from app.ai.planner import plan_session
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
        used = await db.fetch(
            "SELECT moment_id FROM rounds WHERE session_id = $1 AND moment_id IS NOT NULL",
            session_id,
        )
        moment_rows = await db.fetch(
            "SELECT id, kind, item_ids, participant_profile_ids, centroid::text AS centroid FROM moments"
            " WHERE retired_at IS NULL"
        )
        item_rows = await db.fetch(
            """SELECT id, sender_profile_id, participant_profile_ids, body, content_type, media_url,
                      media_description, media_credit, embedding::text AS embedding
               FROM group_context_items
               WHERE safe_for_demo AND (body IS NOT NULL OR media_description IS NOT NULL)
                 AND sender_profile_id IS NOT NULL"""
        )
    items = {
        str(r["id"]): ItemView(
            str(r["id"]),
            str(r["sender_profile_id"]),
            frozenset(str(p) for p in r["participant_profile_ids"]),
            r["body"] or "",
            r["content_type"],
            r["media_url"],
            r["media_description"],
            r["media_credit"],
        )
        for r in item_rows
    }
    item_vectors = {
        str(r["id"]): v for r in item_rows if (v := _vector(r["embedding"])) is not None
    }
    moments = [
        MomentView(
            str(r["id"]),
            r["kind"],
            tuple(str(i) for i in r["item_ids"]),
            frozenset(str(p) for p in r["participant_profile_ids"]),
            _vector(r["centroid"]),
        )
        for r in moment_rows
    ]
    used_ids = {str(r["moment_id"]) for r in used}
    return names, items, moments, member_vectors_from_items(item_vectors, items), used_ids


async def load_interests(pool, names: dict[str, str]) -> dict[str, list[Interest]]:
    """Each player's interests by display name. Muse re-reads a player only when their activity changed."""
    async with pool.acquire() as db:
        rows = await db.fetch(
            """SELECT id, owner_profile_id, kind, visibility, text FROM player_activity
               WHERE owner_profile_id = ANY($1::uuid[]) ORDER BY occurred_at DESC""",
            list(names),
        )
        cached = {
            str(r["profile_id"]): r
            for r in await db.fetch(
                "SELECT profile_id, interests, activity_count FROM player_interests WHERE profile_id = ANY($1::uuid[])",
                list(names),
            )
        }
    activity: dict[str, list[ActivityItem]] = {pid: [] for pid in names}
    for r in rows:
        activity[str(r["owner_profile_id"])].append(
            ActivityItem(str(r["id"]), r["kind"], r["visibility"], r["text"])
        )

    stale = [
        pid
        for pid, items in activity.items()
        if items and (pid not in cached or cached[pid]["activity_count"] != len(items))
    ]
    fresh = dict(
        zip(
            stale,
            await asyncio.gather(*(extract_interests(activity[pid]) for pid in stale)),
            strict=True,
        )
    )
    if fresh:
        async with pool.acquire() as db:
            for pid, found in fresh.items():
                await db.execute(
                    """INSERT INTO player_interests(profile_id, interests, activity_count) VALUES($1, $2::jsonb, $3)
                       ON CONFLICT (profile_id) DO UPDATE SET interests = EXCLUDED.interests,
                           activity_count = EXCLUDED.activity_count, computed_at = now()""",
                    pid,
                    json.dumps([i.__dict__ for i in found]),
                    len(activity[pid]),
                )

    out: dict[str, list[Interest]] = {}
    for pid, name in names.items():
        if pid in fresh:
            out[name] = fresh[pid]
        elif pid in cached:
            out[name] = [
                Interest(i["topic"], i["detail"], tuple(i["evidence"]), i["public"])
                for i in json.loads(cached[pid]["interests"])
            ]
        else:
            out[name] = []
    return out


async def draft_rounds(
    pool, room_id: UUID, session_id: UUID, ordinals: list[int]
) -> list[tuple[int, RoundDraft]]:
    """Shared history (moments) + each player's own interests (and where they overlap or clash), then the
    planner picks the flowchart branch and round types, and Muse writes every round in parallel."""
    names, items, moments, vectors, used = await load_context(pool, room_id, session_id)
    picks = score_moments(moments, items, frozenset(names), vectors, frozenset(used))
    interests = await load_interests(pool, names)
    links = await find_links(interests)
    branch, drafts = await plan_session(picks, links, interests, names, len(ordinals))
    return [
        (o, d.model_copy(update={"branch": branch})) for o, d in zip(ordinals, drafts, strict=True)
    ]


async def draft_round(pool, room_id: UUID, session_id: UUID, ordinal: int) -> RoundDraft | None:
    drafted = await draft_rounds(pool, room_id, session_id, [ordinal])
    return drafted[0][1] if drafted else None


def round_media(options: dict) -> dict | None:
    """The photo or reel a round shows, for the Realtime rounds row and the timeline. None if it's text only."""
    if not options.get("media_url"):
        return None
    return {
        "type": options.get("source_content_type", "photo"),
        "url": options["media_url"],
        "credit": options.get("media_credit"),
        "caption": options.get("quote"),
    }


async def insert_round(db, session_id: UUID, ordinal: int, draft: RoundDraft, phase: str) -> UUID:
    round_id = await db.fetchval(
        """INSERT INTO rounds(session_id, room_id, ordinal, game_type, phase, prompt, options,
                              moment_id, media, opened_at, required_response_count)
           VALUES($1, (SELECT room_id FROM game_sessions WHERE id = $1), $2, $3::game_type,
                  $4::round_phase, $5, $6::jsonb, $7, $8::jsonb,
                  CASE WHEN $4::round_phase = 'answering' THEN now() END,
                  (SELECT count(*)::integer FROM room_members m
                   JOIN game_sessions s ON s.room_id = m.room_id
                   WHERE s.id = $1 AND m.left_at IS NULL))
           ON CONFLICT (session_id, ordinal) DO NOTHING
           RETURNING id""",
        session_id,
        ordinal,
        draft.game_type.value,
        phase,
        draft.prompt,
        json.dumps(
            {
                "choices": draft.options,
                "quote": draft.quote,
                "source_content_type": draft.source_content_type,
                "media_url": draft.media_url,
                "media_credit": draft.media_credit,
            }
        ),
        draft.moment_id,
        json.dumps(
            round_media(
                {
                    "media_url": draft.media_url,
                    "media_credit": draft.media_credit,
                    "source_content_type": draft.source_content_type,
                    "quote": draft.quote,
                }
            )
            or {}
        ),
    )
    if round_id is None:
        return await db.fetchval(
            "SELECT id FROM rounds WHERE session_id = $1 AND ordinal = $2", session_id, ordinal
        )
    await db.execute(
        """INSERT INTO round_answers(round_id, answer, source_item_ids, story_holder_profile_id, reveal_copy)
           VALUES($1, $2::jsonb, $3::uuid[], $4, $5)""",
        round_id,
        json.dumps(draft.answer),
        draft.source_item_ids,
        draft.story_holder_id,
        draft.reveal_copy,
    )
    return round_id


async def announce_round(db, room_id: UUID, round_id: UUID) -> None:
    row = await db.fetchrow(
        "SELECT ordinal, game_type, prompt, options FROM rounds WHERE id = $1", round_id
    )
    options = json.loads(row["options"])
    await db.execute(
        """INSERT INTO timeline_events(room_id, event_type, payload)
           VALUES($1, 'game_prompt', $2::jsonb)""",
        room_id,
        json.dumps(
            {
                "round_id": str(round_id),
                "ordinal": row["ordinal"],
                "game_type": row["game_type"],
                "prompt": row["prompt"],
                "quote": options.get("quote"),
                "options": options["choices"],
                "media": round_media(options),
            }
        ),
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
            "SELECT id FROM rounds WHERE session_id = $1 AND phase = 'pending' ORDER BY ordinal LIMIT 1",
            session_id,
        )
    if pending is None:
        await prefetch_next(pool, room_id, session_id)
        async with pool.acquire() as db:
            pending = await db.fetchval(
                "SELECT id FROM rounds WHERE session_id = $1 AND phase = 'pending' ORDER BY ordinal LIMIT 1",
                session_id,
            )
    async with pool.acquire() as db, db.transaction():
        if pending is None:
            await db.execute(
                "UPDATE game_sessions SET status = 'complete' WHERE id = $1", session_id
            )
            await db.execute(
                """INSERT INTO timeline_events(room_id, event_type, payload)
                   VALUES($1, 'game_reveal', jsonb_build_object('session_id', $2::text, 'game_over', true))""",
                room_id,
                str(session_id),
            )
            return False
        await db.execute(
            "UPDATE rounds SET phase = 'answering', opened_at = now() WHERE id = $1", pending
        )
        await announce_round(db, room_id, pending)
        return True


async def reveal_payload(db, round_id: UUID) -> tuple[dict, UUID | None]:
    """Everything clients need at the reveal, plus who should get the spotlight afterwards."""
    row = await db.fetchrow(
        """SELECT r.game_type, a.reveal_copy, a.story_holder_profile_id, a.answer
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
    if row["game_type"] in ("hot_take", "this_or_that") and len(votes) > 1:
        # Opinion rounds: the spotlight goes to someone who was outvoted, about their own pick.
        fewest = min(votes.values())
        minority = {pick for pick, n in votes.items() if n == fewest}
        story_holder = next(
            r["profile_id"] for r in responses if json.loads(r["value"]) in minority
        )
    elif row["game_type"] == "most_likely_to" and votes:
        top_name = votes.most_common(1)[0][0]
        story_holder = await db.fetchval(
            "SELECT id FROM profiles WHERE display_name = $1", top_name
        )
    answer = json.loads(row["answer"]) if row["answer"] else None
    results = []
    for response in responses:
        chosen = json.loads(response["value"])
        correct = answer is not None and chosen == answer
        results.append(
            {
                "profile_id": str(response["profile_id"]),
                "correct": correct,
                "points": int(correct),
            }
        )
    return {
        "round_id": str(round_id),
        "game_type": row["game_type"],
        "correct_profile_id": str(story_holder) if story_holder else None,
        "message": row["reveal_copy"],
        "results": results,
    }, story_holder
