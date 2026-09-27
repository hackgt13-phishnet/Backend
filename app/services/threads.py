"""Shared demo chat discovery. A thread key is not a private-chat authorization token."""

import re

from fastapi import HTTPException

from app.services.game import GameService


class ThreadGames(GameService):
    @staticmethod
    def validate(key):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
            raise HTTPException(422, "Invalid demo thread key")

    async def games(self, user_id, key):
        self.validate(key)
        actor = await self.profile(user_id)
        mapping = await self.db.fetchrow(
            "SELECT t.room_id,r.host_profile_id FROM demo_threads t LEFT JOIN rooms r ON r.id=t.room_id WHERE t.thread_key=$1",
            key,
        )
        rows = await self.db.fetch(
            """SELECT s.id AS session_id,s.room_id,s.created_at,s.status,r.host_profile_id,
                p.display_name AS host_name,
                EXISTS(SELECT 1 FROM rounds q WHERE q.session_id=s.id AND q.phase IN ('answering','pending')) AS has_unrevealed_rounds,
                EXISTS(SELECT 1 FROM room_members m WHERE m.room_id=r.id AND m.profile_id=$2 AND m.left_at IS NULL) AS is_member,
                EXISTS(SELECT 1 FROM rounds q JOIN private.round_secrets x ON x.round_id=q.id
                       WHERE q.session_id=s.id AND $2=ANY(x.eligible_profile_ids)) AS is_participant
            FROM demo_threads t JOIN rooms r ON r.id=t.room_id
            JOIN profiles p ON p.id=r.host_profile_id JOIN game_sessions s ON s.room_id=r.id
            WHERE t.thread_key=$1 ORDER BY s.created_at DESC,s.id DESC LIMIT 50""",
            key,
            actor,
        )
        return {
            "room_id": mapping["room_id"] if mapping else None,
            "can_start": not mapping
            or mapping["room_id"] is None
            or mapping["host_profile_id"] == actor,
            "games": [dict(r) for r in reversed(rows)],
        }

    async def start_thread(self, user_id, key, name):
        self.validate(key)
        await self.profile(user_id)
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO demo_threads(thread_key) VALUES($1) ON CONFLICT DO NOTHING", key
            )
            room_id = await self.db.fetchval(
                "SELECT room_id FROM demo_threads WHERE thread_key=$1 FOR UPDATE", key
            )
            if room_id is None:
                room = await self.create_room(user_id, name)
                room_id = room["id"]
                await self.db.execute(
                    "UPDATE demo_threads SET room_id=$2 WHERE thread_key=$1", key, room_id
                )
            return await self.start(user_id, room_id)

    async def join_game(self, user_id, key, session_id):
        self.validate(key)
        actor = await self.profile(user_id)
        async with self.db.transaction():
            room = await self.db.fetchrow(
                "SELECT r.* FROM demo_threads t JOIN rooms r ON r.id=t.room_id WHERE t.thread_key=$1 FOR UPDATE OF r",
                key,
            )
            if room is None:
                raise HTTPException(404, "Game not found in this chat")
            session = await self.db.fetchrow(
                "SELECT * FROM game_sessions WHERE id=$1 AND room_id=$2", session_id, room["id"]
            )
            if session is None:
                raise HTTPException(404, "Game not found in this chat")
            await self.enroll(actor, room["id"], session)
            await self.db.execute(
                "INSERT INTO room_members(room_id,profile_id) VALUES($1,$2) "
                "ON CONFLICT(room_id,profile_id) DO UPDATE SET left_at=NULL",
                room["id"],
                actor,
            )
            return {"room": dict(room), "session": dict(session)}
