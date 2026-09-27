"""Record one real game for the X-ray viewer: the real planner, the real Muse calls, the real timing
model, against the real Supabase project, using the recording profiles from record_setup.py so no
teammate's claimed profile is ever touched.

This calls the exact functions production used to call before `/rooms/{id}/sessions` was rewired to
Roshan's fixture rounds (see the PR body for that bug) — draft_rounds, insert_round, announce_round,
tick_room — so what's recorded is the real AI game, even though the live HTTP route can't reach it
right now.

  uv run --env-file .env python scripts/record_game.py
  writes seed/trace/trace.json (read by the X-ray artifact)
"""

import asyncio
import json
import logging
import os
import random
import string
import sys
import time
from pathlib import Path
from uuid import UUID

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["GAME_PACE"] = "demo"  # short conductor windows, so the recording finishes in minutes

ROOT = Path(__file__).resolve().parent.parent
TRACE_DIR = ROOT / "seed" / "trace"
MUSE_TRACE_FILE = TRACE_DIR / "muse_calls.jsonl"
os.environ["MUSE_TRACE_FILE"] = str(MUSE_TRACE_FILE)

from app.ai.conductor import Conductor, pace_from_env
from app.services import game_master
from app.services.game_master import tick_room
from app.services.rounds import ROUNDS_PER_SESSION, announce_round, draft_rounds, insert_round


async def tick_and_drain(pool, conductor: Conductor, room_id: UUID):
    """tick_room fires its follow-up (a Muse nudge, prefetching the next round, opening it) as a
    background task, by design, so a slow model never holds the room's lock. A live server just lets
    that task run on its own time; this script needs it finished before it looks at the DB again."""
    decision = await tick_room(pool, conductor, room_id)
    if game_master._tasks:
        await asyncio.gather(*list(game_master._tasks), return_exceptions=True)
    return decision


CHAT_ACTIVE = [
    "NO WAY IT WAS YOU",
    "i knew it 💀",
    "wait i need context",
    "this is so real",
    "im screaming",
    "explain right now",
    "lmaooo of course",
]


async def setup_room(db, profiles: dict[str, str]) -> tuple[UUID, str]:
    host = profiles["Maya ·rec"]
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    room_id = await db.fetchval(
        "INSERT INTO rooms(name, join_code, host_profile_id) VALUES($1, $2, $3) RETURNING id",
        "X-ray recording",
        code,
        host,
    )
    for pid in profiles.values():
        await db.execute(
            "INSERT INTO room_members(room_id, profile_id, role) VALUES($1, $2, $3)",
            room_id,
            pid,
            "host" if pid == host else "member",
        )
    return room_id, code


async def start_game(pool, db, room_id: UUID) -> UUID:
    """Mirrors the pre-merge /rooms/{id}/sessions handler exactly: real planner, real Muse writers."""
    session_id = await db.fetchval(
        "INSERT INTO game_sessions(room_id, vibe) VALUES($1, 'chaos') RETURNING id", room_id
    )
    drafted = await draft_rounds(pool, room_id, session_id, list(range(1, ROUNDS_PER_SESSION + 1)))
    round_ids = [
        await insert_round(
            db, session_id, ordinal, draft, "answering" if ordinal == 1 else "pending"
        )
        for ordinal, draft in drafted
    ]
    await db.execute(
        "INSERT INTO timeline_events(room_id, event_type, payload) VALUES($1, 'game_started', $2::jsonb)",
        room_id,
        json.dumps(
            {
                "session_id": str(session_id),
                "vibe": "chaos",
                "rounds": len(drafted),
                "branch": drafted[0][1].branch,
            }
        ),
    )
    await announce_round(db, room_id, round_ids[0])
    return session_id


async def submit(db, round_id: UUID, profile_id: UUID, value: str) -> None:
    await db.execute(
        """INSERT INTO round_responses(round_id, profile_id, value) VALUES($1, $2, $3::jsonb)
           ON CONFLICT (round_id, profile_id) DO NOTHING""",
        round_id,
        profile_id,
        json.dumps(value),
    )


async def post_message(db, room_id: UUID, profile_id: UUID, body: str) -> None:
    await db.execute(
        """INSERT INTO timeline_events(room_id, event_type, actor_profile_id, payload)
           VALUES($1, 'message', $2, jsonb_build_object('body', $3::text))""",
        room_id,
        profile_id,
        body,
    )


async def current_round(db, room_id: UUID) -> dict | None:
    return await db.fetchrow(
        """SELECT r.id, r.phase, r.options, r.game_type FROM rounds r JOIN game_sessions s ON s.id = r.session_id
           WHERE s.room_id = $1 AND s.status = 'active' AND r.phase IN ('answering', 'revealed')
           ORDER BY r.ordinal DESC LIMIT 1""",
        room_id,
    )


async def play(pool, conductor: Conductor, room_id: UUID, profiles: dict[str, str]) -> None:
    """One tick_s-paced loop, driven purely off what's actually in the database each time round —
    not off timing assumptions about when a background task (a nudge, opening the next round) lands.
    Each round is a fresh player action or a fresh reveal-chat, gated so replaying a round we've
    already handled is a harmless no-op instead of a second, unwanted round of chatting."""
    ids = list(profiles.values())
    submitted_for: set[str] = set()
    chatted_for: set[str] = set()
    quiet_round = 2  # this one gets no chat, so the demo shows a real conductor NUDGE
    seen_rounds = 0
    while True:
        async with pool.acquire() as db:
            row = await current_round(db, room_id)
        if row is None:
            break
        round_id = str(row["id"])
        if row["phase"] == "answering" and round_id not in submitted_for:
            submitted_for.add(round_id)
            seen_rounds += 1
            options = json.loads(row["options"])["choices"]
            async with pool.acquire() as db:
                for i, pid in enumerate(ids):
                    await submit(db, row["id"], UUID(pid), options[i % len(options)])
        elif (
            row["phase"] == "revealed"
            and round_id not in chatted_for
            and seen_rounds != quiet_round
        ):
            chatted_for.add(round_id)
            async with pool.acquire() as db:
                for i, pid in enumerate(ids):
                    await post_message(db, room_id, UUID(pid), CHAT_ACTIVE[i % len(CHAT_ACTIVE)])
        await tick_and_drain(pool, conductor, room_id)
        await asyncio.sleep(conductor.pace.tick_s)


def load_llm_trace() -> list[dict]:
    if not MUSE_TRACE_FILE.exists():
        return []
    return [json.loads(line) for line in MUSE_TRACE_FILE.read_text().splitlines() if line.strip()]


def match_round_write(
    llm_calls: list[dict], used: set[int], prompt: str | None, reveal: str | None
) -> dict | None:
    """The exact Muse exchange that produced this round: matched by exact text it actually wrote,
    not by guessing timestamps (Postgres and this script's clock aren't the same clock)."""
    for i, call in enumerate(llm_calls):
        if i in used or call.get("kind") != "llm_call":
            continue
        parsed = call.get("parsed") or {}
        if (reveal and parsed.get("reveal") == reveal) or (
            prompt and parsed.get("prompt") == prompt
        ):
            used.add(i)
            return call
    return None


def match_nudge(llm_calls: list[dict], used: set[int], line: str, target_name: str) -> dict | None:
    for i, call in enumerate(llm_calls):
        if (
            i in used
            or call.get("kind") != "llm_call"
            or "quiet after a round" not in call.get("system", "")
        ):
            continue
        try:
            person = json.loads(call["user"]).get("person")
        except (json.JSONDecodeError, TypeError):
            person = None
        if person != target_name:
            continue
        parsed = call.get("parsed") or {}
        if (
            parsed.get("line") == line or True
        ):  # a rejected Muse attempt still belongs to this nudge
            used.add(i)
            return call
    return None


async def build_trace(pool, room_id: UUID, profiles: dict[str, str]) -> dict:
    id_to_name = {v: k for k, v in profiles.items()}
    async with pool.acquire() as db:
        events = [
            dict(r)
            for r in await db.fetch(
                "SELECT * FROM timeline_events WHERE room_id = $1 ORDER BY created_at, id", room_id
            )
        ]
        decisions = [
            dict(r)
            for r in await db.fetch(
                "SELECT * FROM gm_decisions WHERE room_id = $1 ORDER BY created_at, id", room_id
            )
        ]
        rounds = {
            str(r["id"]): dict(r)
            for r in await db.fetch(
                """SELECT r.id, r.ordinal, r.game_type, r.prompt, r.options, a.reveal_copy, a.answer,
                      a.story_holder_profile_id
               FROM rounds r JOIN game_sessions s ON s.id = r.session_id
                 JOIN round_answers a ON a.round_id = r.id
               WHERE s.room_id = $1 ORDER BY r.ordinal""",
                room_id,
            )
        }

    llm_calls = load_llm_trace()
    used: set[int] = set()
    for row in rounds.values():
        options = json.loads(row["options"])
        row["muse"] = match_round_write(llm_calls, used, row["prompt"], row["reveal_copy"])
        row["media"] = (
            {
                "url": options.get("media_url"),
                "credit": options.get("media_credit"),
                "caption": options.get("quote"),
            }
            if options.get("media_url")
            else None
        )
        row["options"] = options["choices"]

    for d in decisions:
        d["created_at"] = d["created_at"].isoformat()
        d["target_name"] = (
            id_to_name.get(str(d["target_profile_id"])) if d["target_profile_id"] else None
        )
        d["features"] = (
            json.loads(d["features"]) if isinstance(d["features"], str) else d["features"]
        )

    for e in events:
        e["created_at"] = e["created_at"].isoformat()
        e["actor_name"] = (
            id_to_name.get(str(e["actor_profile_id"])) if e["actor_profile_id"] else None
        )
        e["payload"] = json.loads(e["payload"]) if isinstance(e["payload"], str) else e["payload"]
        if e["event_type"] == "host_line":
            e["muse"] = match_nudge(
                llm_calls,
                used,
                e["payload"].get("text", ""),
                id_to_name.get(e["payload"].get("target_profile_id"), ""),
            )

    moments = json.loads((ROOT / "seed" / "recording_moments.json").read_text())
    photos = json.loads((ROOT / "seed" / "photos.json").read_text())
    return {
        "recorded_at": time.time(),
        "profiles": [{"id": pid, "name": name} for name, pid in profiles.items()],
        "moments": moments,
        "photos": photos,
        "events": events,
        "decisions": decisions,
        "rounds": list(rounds.values()),
        "llm_calls_total": len(llm_calls),
        "llm_calls_matched": len(used),
        "conductor_source": pace_from_env().name,
    }


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    MUSE_TRACE_FILE.write_text("")  # fresh trace for this recording
    profiles = {
        p["display_name"]: p["id"]
        for p in json.loads((ROOT / "seed" / "recording_items.json").read_text())["profiles"]
    }
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=6)
    conductor = Conductor(pace=pace_from_env())
    async with pool.acquire() as db:
        room_id, code = await setup_room(db, profiles)
        print(f"room {code} · {room_id}")
        session_id = await start_game(pool, db, room_id)
    print(f"game started · session {session_id}")
    await play(pool, conductor, room_id, profiles)
    print("game over, building trace…")
    trace = await build_trace(pool, room_id, profiles)
    (TRACE_DIR / "trace.json").write_text(
        json.dumps(trace, indent=1, ensure_ascii=False, default=str)
    )
    print(
        f"wrote {TRACE_DIR / 'trace.json'} · {len(trace['events'])} events, {len(trace['decisions'])} gm "
        f"decisions, {trace['llm_calls_matched']}/{trace['llm_calls_total']} Muse calls matched to a moment"
    )
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
