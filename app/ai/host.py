"""The one line the game master is allowed to say when a conversation stalls."""

import json
import random

from app.ai.guard import check_host_line
from app.ai.llm import complete_json
from app.ai.rounds import VOICE

NUDGE_SYSTEM = VOICE + (
    " The chat just went quiet after a reveal. Write ONE short line (max 20 words) that hands the "
    "turn to the named person so they tell the story behind it. Use only facts from the round. "
    'Name only that person. JSON: {"line": "..."}'
)
FALLBACKS = [
    "{name}, you've been awfully quiet for someone with a story here",
    "ok {name}, context. now.",
    "{name} explain yourself",
]


async def nudge_line(target: str, all_names: list[str], round_prompt: str, reveal: str,
                     recent_chat: list[str]) -> tuple[str, str]:
    """(line, written_by). Falls back to a template if Muse is down or breaks a rule."""
    reply = await complete_json(NUDGE_SYSTEM, json.dumps({
        "person": target, "round": round_prompt, "reveal": reveal, "last_messages": recent_chat[-6:],
    }, ensure_ascii=False))
    line = str((reply or {}).get("line", "")).strip()
    if line and check_host_line(line, target, all_names) is None:
        return line, "muse"
    return random.choice(FALLBACKS).format(name=target), "template"
