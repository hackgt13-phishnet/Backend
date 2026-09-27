"""Trusted PostgreSQL commands. All room writes lock the room before game rows."""

import asyncio
import base64
import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from app.ai.judge import Answer, Verdict, judge
from app.domain import GameType, PublicRound, RoundDraft, RoundPhase
from app.services.round_fixture import build_rounds
from app.services.rounds import decoded, draft_rounds

log = logging.getLogger(__name__)

AI_DRAFT_TIMEOUT = 45.0  # seconds; past this the game starts with the fixture rounds instead

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


def option_id(option: dict) -> str:
    return str(option.get("id") or option["profile_id"])


def is_judged(secret) -> bool:
    """Opinion rounds have no right answer: the AI judges whose "why" was most interesting."""
    answer = decoded(secret["answer"])
    return isinstance(answer, dict) and bool(answer.get("judge"))


OPEN = "open"  # the value submitted in open rounds, where the answer itself is the typed text


def response_parts(value) -> tuple[str, str | None]:
    value = decoded(value)
    if isinstance(value, dict):
        return str(value.get("choice")), value.get("why")
    return str(value), None


def check_submission(round_row, secret, actor: UUID, value: str, why: str | None = None) -> None:
    if round_row["phase"] != "answering":
        raise HTTPException(409, "Round is not answering")
    if actor not in secret["eligible_profile_ids"]:
        raise HTTPException(403, "Not an eligible respondent")
    if actor in round_row["submitted_profile_ids"]:
        raise HTTPException(409, "Response already submitted")
    options = decoded(round_row["options"])
    if not options:  # open round: the typed take is the answer
        if str(value) != OPEN or not (why or "").strip():
            raise HTTPException(422, "Type your take")
        return
    if str(value) not in {option_id(option) for option in options}:
        raise HTTPException(422, "Answer must be a round option")
    if is_judged(secret) and not (why or "").strip():
        raise HTTPException(422, "Say why in a few words")


def judged_result(secret, responses, verdict: Verdict) -> dict:
    results = []
    for r in responses:
        choice, why = response_parts(r["value"])
        won = str(r["profile_id"]) == verdict.winner_profile_id
        results.append(
            {
                "profile_id": str(r["profile_id"]),
                "correct": won,
                "points": int(won),
                "choice": None if choice == OPEN else choice,
                "why": why,
            }
        )
    return {
        "message": secret["reveal_copy"],
        "results": results,
        "winner_profile_id": verdict.winner_profile_id,
        "shoutout": verdict.shoutout,
        "source": secret.get("source_note"),
    }


def reveal_result(secret, responses, verdict: Verdict | None = None) -> dict:
    eligible = set(secret["eligible_profile_ids"])
    if {r["profile_id"] for r in responses} != eligible:
        raise HTTPException(409, "Waiting for all eligible respondents")
    if verdict is not None:
        return judged_result(secret, responses, verdict)
    answer = decoded(secret["answer"])
    try:
        correct = str(UUID(answer["correct_profile_id"] if isinstance(answer, dict) else answer))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise HTTPException(
            409, "Legacy round answer needs a trusted profile-UUID mapping"
        ) from error
    return {
        "correct_profile_id": correct,
        "source": secret.get("source_note"),
        "message": secret["reveal_copy"],
        "results": [
            {
                "profile_id": str(r["profile_id"]),
                "correct": response_parts(r["value"])[0] == correct,
                "points": int(response_parts(r["value"])[0] == correct),
                "choice": response_parts(r["value"])[0],
            }
            for r in responses
        ],
    }


def ai_round(draft: RoundDraft, names: dict[str, str]) -> dict:
    """An AI draft in the shape `start` stores: public options with ids, and the secret answer."""
    name_to_id = {name: pid for pid, name in names.items()}
    if draft.game_type in (GameType.WHO_SENT_THIS, GameType.MOST_LIKELY_TO) and all(
        o in name_to_id for o in draft.options
    ):
        options = [
            {"id": name_to_id[o], "label": o, "profile_id": name_to_id[o]} for o in draft.options
        ]
    else:
        options = [{"id": chr(ord("a") + n), "label": o} for n, o in enumerate(draft.options)]
    if draft.game_type == GameType.WHO_SENT_THIS and draft.answer in name_to_id:
        answer = {"correct_profile_id": name_to_id[draft.answer]}
    else:
        answer = {"judge": True}
    media = {"quote": draft.quote, "url": draft.media_url}
    if draft.source_content_type == "reel":
        # Shown as a reel card: what the sender wrote with it is its caption, not a quoted message.
        media = {"type": "reel", "caption": draft.quote or "a reel", "url": draft.media_url}
    elif draft.quote or draft.media_url:
        media["type"] = draft.source_content_type
    return {
        "about": draft.about,
        "source_note": draft.source_note,
        "game_type": draft.game_type.value,
        "prompt": draft.prompt,
        "options": options,
        "media": {k: v for k, v in media.items() if v},
        "answer": answer,
        "reveal_copy": draft.reveal_copy,
        "source_item_ids": list(draft.source_item_ids),
        "story_holder_id": draft.story_holder_id or answer.get("correct_profile_id"),
    }


class _SingleConnectionPool:
    """Lets the AI drafter, written against a pool, reuse the request's connection."""

    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def acquire(self):
        yield self.db


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

    async def release_identity(self, user_id: UUID) -> None:
        """Frees the caller's demo profile so another device can claim it. Room memberships
        belong to the profile, so whoever claims it next inherits its chats and scores."""
        await self.db.execute("DELETE FROM demo_identities WHERE user_id=$1", user_id)

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
        if session["status"] != "active":
            raise HTTPException(409, "Not the current active round")
        # Async sessions open every round at once; live sessions play one round at a time.
        if (
            session.get("mode", "live") != "async"
            and session["current_round_ordinal"] != row["ordinal"]
        ):
            raise HTTPException(409, "Not the current active round")
        return actor, row, session

    async def event(self, room_id, event_type, payload, actor=None):
        # clock_timestamp, not now(): several events in one transaction must keep their order.
        row = await self.db.fetchrow(
            "INSERT INTO timeline_events(room_id,event_type,actor_profile_id,payload,created_at) "
            "VALUES($1,$2,$3,$4,clock_timestamp()) RETURNING *",
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

    async def thread_room(self, user_id, thread_key, name):
        """Get or create the group chat's room and make the caller an active member."""
        async with self.db.transaction():
            actor = await self.profile(user_id)
            room = await self.db.fetchrow(
                "SELECT * FROM rooms WHERE thread_key=$1 FOR UPDATE", thread_key
            )
            if room is None:
                room = await self.db.fetchrow(
                    "INSERT INTO rooms(name,join_code,host_profile_id,thread_key) VALUES($1,$2,$3,$4) "
                    "ON CONFLICT (thread_key) DO NOTHING RETURNING *",
                    name,
                    secrets.token_hex(4).upper(),
                    actor,
                    thread_key,
                )
                if room is None:
                    room = await self.db.fetchrow(
                        "SELECT * FROM rooms WHERE thread_key=$1 FOR UPDATE", thread_key
                    )
            await self.db.execute(
                "INSERT INTO room_members(room_id,profile_id,role) VALUES($1,$2,$3) "
                "ON CONFLICT(room_id,profile_id) DO UPDATE SET left_at=NULL "
                "WHERE room_members.left_at IS NOT NULL",
                room["id"],
                actor,
                "host" if room["host_profile_id"] == actor else "member",
            )
            return dict(room)

    async def send_game(self, user_id, thread_key, name, mode="async"):
        """GamePigeon-style: whoever sends the game in the chat hosts that session."""
        room = await self.thread_room(user_id, thread_key, name)
        async with self.db.transaction():
            actor, room = await self.room_access(room["id"], user_id, lock=True)
            if await self.db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM game_sessions WHERE room_id=$1 AND status='active')",
                room["id"],
            ):
                raise HTTPException(409, "A game is already running in this chat")
            if room["host_profile_id"] != actor:
                room = await self.db.fetchrow(
                    "UPDATE rooms SET host_profile_id=$2 WHERE id=$1 RETURNING *", room["id"], actor
                )
                await self.db.execute(
                    "UPDATE room_members SET role=(CASE WHEN profile_id=$2 THEN 'host' ELSE 'member' END)::room_role "
                    "WHERE room_id=$1 AND role <> (CASE WHEN profile_id=$2 THEN 'host' ELSE 'member' END)::room_role",
                    room["id"],
                    actor,
                )
        started = await self.start(user_id, room["id"], mode)
        return {"room": dict(room), **started}

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

    async def active_players(self, room_id):
        return await self.db.fetch(
            "SELECT p.id,p.display_name FROM profiles p JOIN room_members m ON m.profile_id=p.id "
            "WHERE m.room_id=$1 AND m.left_at IS NULL ORDER BY p.id",
            room_id,
        )

    async def ai_drafts(self, room_id, players) -> list[dict] | None:
        """Muse writes the rounds from these players' shared history and interests. None = use the fixture."""
        names = {str(p["id"]): p["display_name"] for p in players}
        try:
            drafted = await asyncio.wait_for(
                draft_rounds(
                    _SingleConnectionPool(self.db),
                    room_id,
                    uuid.uuid4(),
                    [1, 2, 3],
                    names,
                    chaos=True,
                ),
                AI_DRAFT_TIMEOUT,
            )
        except Exception:
            log.exception("AI round drafting failed; using fixture rounds")
            return None
        return [ai_round(draft, names) for _, draft in drafted]

    async def start(self, user_id, room_id, mode="live"):
        drafts = None
        # Both modes get AI-written rounds; live games are then paced by the game master.
        await self.room_access(room_id, user_id, host=True)
        drafted_for = await self.active_players(room_id)
        if 2 <= len(drafted_for) <= 6:
            # Drafting takes seconds, so it runs before the transaction takes any locks.
            drafts = await self.ai_drafts(room_id, drafted_for)
        async with self.db.transaction():
            await self.room_access(room_id, user_id, host=True, lock=True)
            if await self.db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM game_sessions WHERE room_id=$1 AND status='active')",
                room_id,
            ):
                raise HTTPException(409, "A session is already active")
            players = await self.active_players(room_id)
            if len(players) < 2:
                raise HTTPException(
                    409,
                    "You're the only one in this chat so far. Send someone the link, then start the game.",
                )
            if len(players) > 6:
                raise HTTPException(409, "Chaos supports up to 6 players in a chat")
            if drafts is None or [p["id"] for p in players] != [p["id"] for p in drafted_for]:
                drafts = build_rounds(players)
            player_ids = [p["id"] for p in players]
            session = await self.db.fetchrow(
                "INSERT INTO game_sessions(room_id,vibe,mode) VALUES($1,'chaos',$2) RETURNING *",
                room_id,
                mode,
            )
            opened = []
            for ordinal, draft in enumerate(drafts, 1):
                is_open = mode == "async" or ordinal == 1
                # Whoever sent or posted it sits out their own "who sent this?" round.
                author = str(draft["answer"].get("correct_profile_id") or "")
                round_players = [p for p in player_ids if str(p) != author] or player_ids
                row = await self.db.fetchrow(
                    "INSERT INTO rounds(session_id,room_id,ordinal,game_type,phase,prompt,options,media,"
                    "required_response_count,opened_at,player_profile_ids,about) VALUES($1,$2,$3,$10::game_type,$4,"
                    "$5,$6,$7,$8,CASE WHEN $9::boolean THEN now() END,$11,$12) RETURNING *",
                    session["id"],
                    room_id,
                    ordinal,
                    "answering" if is_open else "pending",
                    draft["prompt"],
                    draft["options"],
                    draft["media"],
                    len(round_players),
                    is_open,
                    draft.get("game_type", "who_sent_this"),
                    round_players,
                    draft.get("about"),
                )
                await self.db.execute(
                    "INSERT INTO private.round_secrets(round_id,answer,reveal_copy,eligible_profile_ids,"
                    "source_item_ids,source_note) VALUES($1,$2,$3,$4,$5,$6)",
                    row["id"],
                    draft["answer"],
                    draft["reveal_copy"],
                    round_players,
                    draft.get("source_item_ids", []),
                    draft.get("source_note"),
                )
                await self.db.execute(
                    "INSERT INTO round_answers(round_id,answer,source_item_ids,story_holder_profile_id,reveal_copy) "
                    "VALUES($1,$2,$3,$4,$5)",
                    row["id"],
                    None if draft["answer"].get("judge") else draft["answer"],
                    draft.get("source_item_ids", []),
                    draft.get("story_holder_id", draft["answer"].get("correct_profile_id")),
                    draft["reveal_copy"],
                )
                if is_open:
                    opened.append(public_round(row))
            first = opened[0]
            await self.event(
                room_id, "game_started", {"session_id": str(session["id"]), "mode": mode}
            )
            for public in opened:
                await self.event(room_id, "game_prompt", public)
            return {"session": dict(session), "current_round": first}

    async def submit(self, user_id, round_id, value, why=None):
        needs_judging = False
        async with self.db.transaction():
            actor, row, session = await self.round_access(round_id, user_id)
            secret = await self.db.fetchrow(
                "SELECT * FROM private.round_secrets WHERE round_id=$1", round_id
            )
            check_submission(row, secret, actor, value, why)
            await self.db.execute(
                "INSERT INTO round_responses(round_id,profile_id,value) VALUES($1,$2,$3)",
                round_id,
                actor,
                {"choice": str(value), "why": why.strip()} if why and why.strip() else str(value),
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
            result = {"accepted": True, **status}
            if session.get("mode", "live") == "async" and len(row["submitted_profile_ids"]) >= len(
                secret["eligible_profile_ids"]
            ):
                if is_judged(secret):
                    needs_judging = True
                else:
                    result.update(await self.settle_async_round(row, secret, session))
        if needs_judging:
            # The model call takes seconds, so it happens between transactions, holding no locks.
            result.update(await self.judge_and_settle(user_id, round_id))
        return result

    async def answers(self, row) -> list[Answer]:
        """Everyone's answer to a round, in the shape the AI judge reads."""
        labels = {option_id(o): o["label"] for o in decoded(row["options"])}
        responses = await self.db.fetch(
            "SELECT rr.profile_id,rr.value,p.display_name FROM round_responses rr "
            "JOIN profiles p ON p.id=rr.profile_id WHERE rr.round_id=$1 ORDER BY rr.profile_id",
            row["id"],
        )
        out = []
        for r in responses:
            choice, why = response_parts(r["value"])
            out.append(
                Answer(str(r["profile_id"]), r["display_name"], labels.get(choice, choice), why or "")
            )
        return out

    async def judge_and_settle(self, user_id, round_id) -> dict:
        prompt = await self.db.fetchval("SELECT prompt FROM rounds WHERE id=$1", round_id)
        options = decoded(
            await self.db.fetchval("SELECT options FROM rounds WHERE id=$1", round_id)
        )
        labels = {option_id(o): o["label"] for o in options}
        responses = await self.db.fetch(
            "SELECT rr.profile_id,rr.value,p.display_name FROM round_responses rr "
            "JOIN profiles p ON p.id=rr.profile_id WHERE rr.round_id=$1 ORDER BY rr.profile_id",
            round_id,
        )
        answers = []
        for r in responses:
            choice, why = response_parts(r["value"])
            answers.append(
                Answer(
                    str(r["profile_id"]), r["display_name"], labels.get(choice, choice), why or ""
                )
            )
        verdict = await judge(prompt, answers)
        async with self.db.transaction():
            _, row, session = await self.round_access(round_id, user_id)
            if row["phase"] != "answering":
                return {}
            secret = await self.db.fetchrow(
                "SELECT * FROM private.round_secrets WHERE round_id=$1", round_id
            )
            return await self.settle_async_round(row, secret, session, verdict)

    async def settle_async_round(
        self, row, secret, session, verdict: Verdict | None = None
    ) -> dict:
        """The last answer reveals the round; the last reveal ends the game. No timers, no host."""
        responses = await self.db.fetch(
            "SELECT profile_id,value FROM round_responses WHERE round_id=$1 ORDER BY profile_id",
            row["id"],
        )
        revealed = await self.db.fetchrow(
            "UPDATE rounds SET phase='revealed',reveal=$2,revealed_at=now() WHERE id=$1 RETURNING *",
            row["id"],
            reveal_result(secret, responses, verdict),
        )
        public = public_round(revealed)
        await self.event(row["room_id"], "game_reveal", public)
        if await self.db.fetchval(
            "SELECT EXISTS(SELECT 1 FROM rounds WHERE session_id=$1 AND phase IN ('pending','answering'))",
            session["id"],
        ):
            return {"round": public}
        await self.db.execute(
            "UPDATE rounds SET phase='complete' WHERE session_id=$1 AND phase='revealed'",
            session["id"],
        )
        finished = await self.db.fetchrow(
            "UPDATE game_sessions SET status='complete' WHERE id=$1 RETURNING *", session["id"]
        )
        await self.event(row["room_id"], "game_over", {"session_id": str(session["id"])})
        return {"round": public, "session": dict(finished)}

    async def reveal(self, user_id, round_id):
        async with self.db.transaction():
            _, row, _ = await self.round_access(round_id, user_id, host=True)
            return await self.reveal_current(row)

    async def reveal_current(self, row):
        """Caller holds the room lock and transaction (API or game master)."""
        require_transition(row["phase"], RoundPhase.REVEALED)
        secret = await self.db.fetchrow(
            "SELECT * FROM private.round_secrets WHERE round_id=$1", row["id"]
        )
        responses = await self.db.fetch(
            "SELECT profile_id,value FROM round_responses WHERE round_id=$1 ORDER BY profile_id",
            row["id"],
        )
        # Opinion rounds (the AI's Hot Takes) have no right answer: the AI judge picks the winner.
        verdict = await judge(row["prompt"], await self.answers(row)) if is_judged(secret) else None
        result = reveal_result(secret, responses, verdict)
        updated = await self.db.fetchrow(
            "UPDATE rounds SET phase='revealed',revealed_at=now(),reveal=$2 WHERE id=$1 RETURNING *",
            row["id"],
            result,
        )
        public = public_round(updated)
        await self.event(row["room_id"], "game_reveal", public)
        return public

    async def advance(self, user_id, round_id):
        async with self.db.transaction():
            _, row, session = await self.round_access(round_id, user_id, host=True)
            require_transition(row["phase"], RoundPhase.COMPLETE)
            # Use the same policy as the timer, under the same room lock.
            from app.ai.conductor import Action, Conductor, pace_from_env
            from app.services.game_master import load_state

            loaded = await load_state(self.db, row["room_id"])
            if loaded is None or loaded[1] != row["id"]:
                raise HTTPException(409, "Not the current active round")
            decision = Conductor(pace=pace_from_env()).decide(loaded[0])
            if decision.action != Action.NEXT_ROUND:
                raise HTTPException(409, "Waiting for the conversation to wind down")
            return await self.advance_current(row, session)

    async def advance_current(self, row, session):
        """Caller holds the room lock and has passed the conductor progression gate."""
        require_transition(row["phase"], RoundPhase.COMPLETE)
        completed = await self.db.fetchrow(
            "UPDATE rounds SET phase='complete' WHERE id=$1 RETURNING *", row["id"]
        )
        next_round = await self.db.fetchrow(
            "SELECT * FROM rounds WHERE session_id=$1 AND ordinal=$2",
            session["id"],
            row["ordinal"] + 1,
        )
        if next_round:
            require_transition(next_round["phase"], RoundPhase.ANSWERING)
            current = await self.db.fetchrow(
                "UPDATE rounds SET phase='answering',opened_at=now() WHERE id=$1 RETURNING *",
                next_round["id"],
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
