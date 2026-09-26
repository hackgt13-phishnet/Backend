"""The one line the game master is allowed to say when a conversation stalls."""

import json
import random

from app.ai.guard import check_host_line
from app.ai.llm import complete_json
from app.ai.rounds import VOICE

NUDGE_SYSTEM = VOICE + (
    " The chat just went quiet after a round. Write ONE short line (max 20 words) that hands the turn to "
    "the named person with a specific, easy reason to talk:\n"
    "- who_sent_this: they sent the quoted message, ask for the story behind it\n"
    "- most_likely_to: the group picked them, let them respond to the verdict\n"
    "- hot_take / this_or_that: ask about THEIR OWN pick (use it), especially if they were outvoted\n"
    "Never ask them to defend a take they voted against. Don't just repeat the reveal. "
    'Use only facts from the round. Name only that person. JSON: {"line": "..."}'
)
FALLBACKS = {
    "who_sent_this": ["{name}, you sent it. context. now.", "ok {name}, what was going on when you sent that"],
    "most_likely_to": ["{name}, the people have spoken. any last words?", "{name} you got picked. defend yourself"],
    "pick": ["{name}, you picked {pick}. make your case", "{name} really said {pick}. explain"],
    "other": ["{name}, you've been awfully quiet for someone with a story here", "ok {name}, context. now."],
}


async def nudge_line(target: str, all_names: list[str], round_prompt: str, reveal: str,
                     recent_chat: list[str], game_type=None, their_pick: str | None = None,
                     votes: dict[str, str] | None = None) -> tuple[str, str]:
    """(line, written_by). Falls back to a template if Muse is down or breaks a rule."""
    kind = getattr(game_type, "value", game_type)
    context = {"person": target, "round_type": kind, "round": round_prompt, "reveal": reveal,
               "their_pick": their_pick, "how_the_room_voted": votes, "last_messages": recent_chat[-6:]}
    reply = await complete_json(NUDGE_SYSTEM, json.dumps(context, ensure_ascii=False))
    line = str((reply or {}).get("line", "")).strip()
    if line and check_host_line(line, target, all_names) is None:
        return line, "muse"
    if kind in ("hot_take", "this_or_that") and their_pick:
        bucket = "pick"
    else:
        bucket = kind if kind in FALLBACKS else "other"
    return random.choice(FALLBACKS[bucket]).format(name=target, pick=their_pick), "template"
