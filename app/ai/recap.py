"""One closing line when a game ends: send the group back to their chat with something to keep talking about."""

import json

from app.ai.guard import check_round_text, names_in
from app.ai.llm import complete_json
from app.ai.rounds import VOICE

RECAP_SYSTEM = VOICE + (
    " The game just ended. Write ONE closing line (max 25 words) that sends the group back to their "
    "chat with something to keep talking about. Use only what happened in the rounds below; the round "
    "with the most messages after its reveal is what got people talking. Don't list the rounds. "
    'Name at most two people. JSON: {"line": "..."}'
)
FALLBACK = "gg. back to your regularly scheduled chaos"


def check_recap(line: str, names: list[str]) -> str | None:
    """None if the line is fine, otherwise why it was rejected."""
    if problem := check_round_text(line, 200):
        return problem
    if len(names_in(line, names)) > 2:
        return "names too many people"
    return None


async def recap_line(rounds: list[dict], names: list[str]) -> tuple[str, str]:
    """(line, written_by). `rounds`: game_type, prompt, reveal, spotlight, messages_after_reveal."""
    reply = await complete_json(RECAP_SYSTEM, json.dumps({"rounds": rounds}, ensure_ascii=False))
    line = str((reply or {}).get("line", "")).strip()
    if line and check_recap(line, names) is None:
        return line, "muse"
    return FALLBACK, "template"
