"""Muse writes rounds from a picked moment. The data decides the answer; the model only writes words."""

import json
import random
from uuid import UUID

from app.ai.guard import check_round_text
from app.ai.llm import complete_json
from app.ai.picker import Pick
from app.domain import GameType, RoundDraft

VOICE = (
    "You're the host of a party game inside a college friend group's Instagram chat. "
    "Write like they text: lowercase, short, funny, a little unhinged, never cringe or corporate. "
    "Swearing is fine. Never mention health, drinking, relationships, religion, politics or money. "
    "Reply with JSON only."
)

WHO_SENT_SYSTEM = VOICE + (
    ' Task: pick the ONE message that makes the best "who sent this?" round (funny, specific, '
    'not obvious from the text itself) and write a one-line reveal for after everyone guesses. '
    'JSON: {"item_id": "...", "reveal": "..."}. The reveal must not invent facts.'
)
MOST_LIKELY_SYSTEM = VOICE + (
    ' Task: write one "who\'s most likely to..." question inspired by these messages, about the whole '
    'group (never name anyone), plus a reveal line for when the votes come in. '
    'JSON: {"prompt": "who\'s most likely to ...?", "reveal": "..."}'
)


def game_for(ordinal: int) -> GameType:
    return GameType.WHO_SENT_THIS if ordinal % 2 == 1 else GameType.MOST_LIKELY_TO


def moment_preference(game: GameType) -> str:
    """Guessing games need a split room; vote games need common ground."""
    return "split" if game == GameType.WHO_SENT_THIS else "shared"


async def who_sent_this(pick: Pick, names: dict[str, str], rng: random.Random) -> RoundDraft:
    candidates = [i for i in pick.items if len(i.body) >= 12] or list(pick.items)
    reply = await complete_json(WHO_SENT_SYSTEM, json.dumps({
        "moment_kind": pick.moment.kind,
        "messages": [{"item_id": i.id, "text": i.body} for i in candidates[:20]],
    }, ensure_ascii=False))
    by_id = {i.id: i for i in candidates}
    chosen = by_id.get((reply or {}).get("item_id"))
    reveal = (reply or {}).get("reveal", "")
    written_by = "muse"
    if chosen is None or check_round_text(reveal):
        chosen, written_by = rng.choice(candidates), "template"
        reveal = f"it was {names[chosen.sender_id]} 💀"
    return RoundDraft(
        game_type=GameType.WHO_SENT_THIS,
        prompt="who sent this?",
        quote=chosen.body,
        source_content_type=chosen.content_type,
        media_url=chosen.media_url,
        options=[names[m] for m in sorted(pick.p_known, key=names.get)],
        answer=names[chosen.sender_id],
        source_item_ids=[UUID(chosen.id)],
        reveal_copy=reveal,
        moment_id=UUID(pick.moment.id),
        story_holder_id=UUID(chosen.sender_id),
        written_by=written_by,
    )


async def most_likely_to(pick: Pick, names: dict[str, str], rng: random.Random) -> RoundDraft:
    reply = await complete_json(MOST_LIKELY_SYSTEM, json.dumps({
        "moment_kind": pick.moment.kind,
        "messages": [i.body for i in pick.items[:15]],
    }, ensure_ascii=False))
    prompt, reveal = (reply or {}).get("prompt", ""), (reply or {}).get("reveal", "")
    ok = (
        prompt.lower().startswith(("who's most likely to", "whos most likely to", "who is most likely to"))
        and not check_round_text(prompt, 160) and not check_round_text(reveal)
        and not any(n.lower() in prompt.lower() for n in names.values())
    )
    written_by = "muse" if ok else "template"
    if not ok:
        prompt = rng.choice([
            "who's most likely to bring this up again at the worst possible time?",
            "who's most likely to be the main character of this story?",
            "who's most likely to still be talking about this in 5 years?",
        ])
        reveal = "the people have spoken"
    return RoundDraft(
        game_type=GameType.MOST_LIKELY_TO,
        prompt=prompt,
        options=[names[m] for m in sorted(pick.p_known, key=names.get)],
        answer=None,
        source_item_ids=[UUID(i.id) for i in pick.items[:8]],
        reveal_copy=reveal,
        moment_id=UUID(pick.moment.id),
        written_by=written_by,
    )


async def write_round(ordinal: int, pick: Pick, names: dict[str, str], seed: int = 0) -> RoundDraft:
    rng = random.Random(f"{pick.moment.id}:{ordinal}:{seed}")
    if game_for(ordinal) == GameType.WHO_SENT_THIS:
        return await who_sent_this(pick, names, rng)
    return await most_likely_to(pick, names, rng)
