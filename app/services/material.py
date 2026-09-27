"""Consent: a player sees what the game may use from them, and takes anything out before it's used."""

from uuid import UUID

from fastapi import HTTPException


async def my_material(db, profile_id: UUID) -> dict:
    """Everything of theirs a round could draw on: what they sent in shared chats, and their own activity."""
    excluded = {
        r["item_id"]
        for r in await db.fetch(
            "SELECT item_id FROM material_exclusions WHERE profile_id = $1", profile_id
        )
    }
    shared = await db.fetch(
        """SELECT id, content_type, body, media_url, media_description, occurred_at
           FROM group_context_items
           WHERE sender_profile_id = $1 AND safe_for_demo
             AND (body IS NOT NULL OR media_description IS NOT NULL)
           ORDER BY occurred_at DESC""",
        profile_id,
    )
    activity = await db.fetch(
        """SELECT id, kind, visibility, text, occurred_at FROM player_activity
           WHERE owner_profile_id = $1 ORDER BY occurred_at DESC""",
        profile_id,
    )
    return {
        "shared": [{**dict(r), "excluded": r["id"] in excluded} for r in shared],
        "activity": [{**dict(r), "excluded": r["id"] in excluded} for r in activity],
    }


async def set_excluded(db, profile_id: UUID, item_id: UUID, excluded: bool) -> None:
    """Only the person an item belongs to can take it out (or put it back)."""
    owns = await db.fetchval(
        """SELECT EXISTS(SELECT 1 FROM group_context_items WHERE id = $1 AND sender_profile_id = $2)
               OR EXISTS(SELECT 1 FROM player_activity WHERE id = $1 AND owner_profile_id = $2)""",
        item_id,
        profile_id,
    )
    if not owns:
        raise HTTPException(404, "Not one of your items")
    if excluded:
        await db.execute(
            "INSERT INTO material_exclusions(profile_id, item_id) VALUES($1, $2) ON CONFLICT DO NOTHING",
            profile_id,
            item_id,
        )
    else:
        await db.execute(
            "DELETE FROM material_exclusions WHERE profile_id = $1 AND item_id = $2",
            profile_id,
            item_id,
        )
