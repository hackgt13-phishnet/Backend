"""Replay a scripted post-reveal chat and print what the conductor decides every 5 seconds.

uv run python scripts/simulate_conductor.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.conductor import Action, Conductor, RoomState, pace_from_env
from app.ai.conductor_features import Turn
from app.domain import RoundPhase

MEMBERS = frozenset({"Maya", "Dev", "Sam", "Ana", "Kofi"})
REVEALED_AT = 0.0

# Who Sent This? reveal: it was Kofi's rice cooker photo. The room reacts, then fades.
CHAT = [
    (3, "Ana", "NO WAY IT WAS KOFI", False),
    (6, "Sam", "💀💀💀", False),
    (9, "Maya", "i said dev bc of the caption", False),
    (13, "Dev", "why would i post a rice cooker", True),
    (18, "Ana", "the rice cooker has a NAME tho", False),
    (26, "Sam", "wait what's its name", True),
    (38, "Maya", "lmaooo", False),
    (57, "Ana", "i can't", False),
]


def main() -> None:
    conductor = Conductor(pace=pace_from_env())
    print(f"conductor: {conductor.source} · pace: {conductor.pace.name}\n")
    print("  t     P(quiet)  decision")
    turns: list[Turn] = []
    step = int(conductor.pace.tick_s)
    for tick in range(0, 181, step):
        for ts, who, text, question in CHAT:
            if tick - step < ts <= tick:
                turns.append(
                    Turn(ts=ts, sender=who, length=len(text), is_question=question, has_media=False)
                )
                print(f"  {ts:>3}s  {who:>5}: {text}")
        state = RoomState(
            RoundPhase.REVEALED,
            now=tick,
            member_ids=MEMBERS,
            revealed_at=REVEALED_AT,
            turns=tuple(turns),
            story_holder_id="Kofi",
        )
        d = conductor.decide(state)
        p = f"{d.p_silence:.2f}" if d.p_silence is not None else "  – "
        marker = "  ◀" if d.action != Action.WAIT else ""
        print(f"  {tick:>3}s  {p:>7}   {d.action.value:10} {d.reason}{marker}")
        if d.action != Action.WAIT:
            target = f" → hand the turn to {d.target_id}" if d.target_id else ""
            print(f"\n  first time the game master acts: {tick}s after the reveal{target}")
            break


if __name__ == "__main__":
    main()
