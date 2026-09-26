"""Muse writes rounds from a picked moment. The data decides the answer; the model only writes words."""

import json
import random
import uuid
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


# ---- rounds from interests: players' own activity, for rooms with little shared history ----

HOT_TAKE_SYSTEM = VOICE + (
    " Task: write ONE spicy but fair hot take about this topic that these friends will split on "
    "(a statement, not a question, under 18 words, never naming anyone), plus a reveal line for when "
    'the votes land. JSON: {"take": "...", "reveal": "..."}'
)
THIS_OR_THAT_SYSTEM = VOICE + (
    " Task: write ONE this-or-that question about this topic that these friends would actually argue "
    "about, with two short options (max 5 words each), plus a reveal line. Never name anyone. "
    "If kind is solo, it's one friend's interest: ask the whole room about the topic, don't write it at them. "
    'JSON: {"prompt": "...", "a": "...", "b": "...", "reveal": "..."}'
)
AGREE = ["agree", "disagree"]

# Low-stakes rounds for rooms with nothing usable. Answers teach the game what the group is into.
GENERAL = [
    ("this or that: be early to everything or always 10 min late?", ["always early", "always late"]),
    ("this or that: 3am drive-thru run or 8am brunch?", ["3am drive-thru", "8am brunch"]),
    ("this or that: group trip planner or the one who just shows up?", ["planner", "just shows up"]),
    ("this or that: voice notes or text walls?", ["voice notes", "text walls"]),
]


def _named(text: str, names: dict[str, str]) -> bool:
    return any(n.lower() in text.lower() for n in names.values())


async def hot_take(link, names: dict[str, str], name_to_id: dict[str, str]) -> RoundDraft:
    reply = await complete_json(HOT_TAKE_SYSTEM, json.dumps(
        {"topic": link.topic, "kind": link.kind, "angle": link.angle}, ensure_ascii=False))
    take, reveal = str((reply or {}).get("take", "")), str((reply or {}).get("reveal", ""))
    ok = take and not check_round_text(take, 160) and not check_round_text(reveal) and not _named(take, names)
    if not ok:
        take, reveal = f"hot take: {link.topic} is overrated", "the chat is divided"
    holder = next(iter(link.players))
    return RoundDraft(
        game_type=GameType.HOT_TAKE, prompt=take, options=AGREE, answer=None,
        source_item_ids=[uuid.uuid4()], reveal_copy=reveal, source="interest",
        story_holder_id=UUID(name_to_id[holder]), written_by="muse" if ok else "template",
    )


async def this_or_that(link, names: dict[str, str], name_to_id: dict[str, str]) -> RoundDraft:
    reply = await complete_json(THIS_OR_THAT_SYSTEM, json.dumps(
        {"topic": link.topic, "kind": link.kind, "angle": link.angle}, ensure_ascii=False))
    r = reply or {}
    prompt, a, b, reveal = (str(r.get(k, "")) for k in ("prompt", "a", "b", "reveal"))
    ok = (
        prompt and a and b and a.lower() != b.lower()
        and all(len(x.split()) <= 5 for x in (a, b))
        and not any(check_round_text(x, 160) for x in (prompt, a, b, reveal))
        and not _named(prompt + a + b, names)
    )
    if not ok:
        prompt, a, b, reveal = f"{link.topic}: overrated or underrated?", "overrated", "underrated", "the people have spoken"
    holder = next(iter(link.players))
    return RoundDraft(
        game_type=GameType.THIS_OR_THAT, prompt=prompt, options=[a, b], answer=None,
        source_item_ids=[uuid.uuid4()], reveal_copy=reveal, source="interest",
        story_holder_id=UUID(name_to_id[holder]), written_by="muse" if ok else "template",
    )


def general_round(ordinal: int) -> RoundDraft:
    prompt, options = GENERAL[(ordinal - 1) % len(GENERAL)]
    return RoundDraft(
        game_type=GameType.THIS_OR_THAT, prompt=prompt, options=options, answer=None,
        source_item_ids=[uuid.uuid4()], reveal_copy="noted. the game master is taking notes",
        source="general", written_by="template",
    )
