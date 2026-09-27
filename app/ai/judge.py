"""Muse judges opinion rounds: whose one-line "why" was the most interesting. The winner gets the point."""

import json
from dataclasses import dataclass

from app.ai.guard import is_sensitive
from app.ai.llm import complete_json
from app.ai.rounds import VOICE

JUDGE_SYSTEM = VOICE + (
    " Task: friends each picked a side and wrote one line on why. Pick the ONE most interesting answer "
    "(funniest, most specific or most unexpected; not the longest, and ignore which side it's on), "
    "and write a short shout-out (under 20 words) saying why it won. You may use the winner's name. "
    'JSON: {"winner": "<answer id>", "shoutout": "..."}'
)


@dataclass(frozen=True)
class Answer:
    profile_id: str
    name: str
    choice: str  # the option label they picked
    why: str


@dataclass(frozen=True)
class Verdict:
    winner_profile_id: str
    shoutout: str
    written_by: str  # "muse" or "template"


def fallback(answers: list[Answer]) -> Verdict:
    """Every model failed: the most detailed answer wins, deterministically."""
    best = max(answers, key=lambda a: (len(set(a.why.lower().split())), a.profile_id))
    return Verdict(best.profile_id, f"{best.name} said it best", "template")


async def judge(prompt: str, answers: list[Answer]) -> Verdict:
    if not answers:
        raise ValueError("Nothing to judge")
    ids = {f"a{n}": a for n, a in enumerate(answers, 1)}
    reply = await complete_json(
        JUDGE_SYSTEM,
        json.dumps(
            {
                "question": prompt,
                "answers": [
                    {"id": key, "name": a.name, "picked": a.choice, "why": a.why}
                    for key, a in ids.items()
                ],
            },
            ensure_ascii=False,
        ),
    )
    winner = ids.get(str((reply or {}).get("winner", "")))
    shoutout = str((reply or {}).get("shoutout", "")).strip()
    if winner is None or not shoutout or len(shoutout) > 160 or is_sensitive(shoutout):
        return fallback(answers)
    return Verdict(winner.profile_id, shoutout, "muse")
