"""Play the AI side of a game offline: pick moments for a room and have Muse write the rounds.

Uses seed/ output from build_memory.py. Set META_MUSE_API_KEY to hear Muse; without it, templates run.

  uv run python scripts/demo_rounds.py                       # all five friends
  uv run python scripts/demo_rounds.py --room Maya Sam Dev   # a smaller room
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.host import nudge_line
from app.ai.llm import models_from_env
from app.ai.picker import ItemView, MomentView, member_vectors_from_items, pick_moment
from app.ai.rounds import game_for, moment_preference, write_round
from app.domain import ROUNDS_PER_SESSION

SEED = Path(__file__).resolve().parent.parent / "seed"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--room", nargs="+", default=["Maya", "Dev", "Sam", "Ana", "Kofi"])
    args = parser.parse_args()

    data = json.loads((SEED / "group_items.json").read_text())
    moments_json = json.loads((SEED / "moments.json").read_text())
    emb = np.load(SEED / "item_embeddings.npz")
    names = {p["id"]: p["display_name"] for p in data["profiles"] if p["display_name"] in args.room}
    members = frozenset(names)

    items = {
        r["id"]: ItemView(r["id"], r["sender_profile_id"], frozenset(r["participant_profile_ids"]), r["body"])
        for r in data["items"] if r["safe_for_demo"]
    }
    item_vectors = dict(zip(emb["ids"].tolist(), emb["vectors"], strict=True))
    member_vectors = member_vectors_from_items(item_vectors, items)
    moments = [MomentView(m["id"], m["kind"], tuple(m["item_ids"]), frozenset(m["participant_profile_ids"]),
                          np.array(m["centroid"])) for m in moments_json]
    labels = {m["id"]: m["label"] for m in moments_json}

    models = models_from_env()
    print(f"room: {', '.join(sorted(names.values()))}")
    print(f"writer: {', '.join(m.model for m in models) or 'templates only (no LLM key set)'}\n")

    used: set[str] = set()
    for ordinal in range(1, ROUNDS_PER_SESSION + 1):
        pick = pick_moment(moments, items, members, member_vectors, frozenset(used),
                           want=moment_preference(game_for(ordinal)))
        if pick is None:
            print("no more moments this room can play")
            break
        used.add(pick.moment.id)
        draft = await write_round(ordinal, pick, names)
        knows = ", ".join(f"{names[m]} {p:.0%}" for m, p in sorted(pick.p_known.items(), key=lambda kv: -kv[1]))
        print(f"round {ordinal} · {draft.game_type.value} · moment: {labels[pick.moment.id]} "
              f"({pick.moment.kind}) · split {pick.split:.2f} · written by {draft.written_by}")
        print(f"  who probably knows it: {knows}")
        print(f"  prompt: {draft.prompt}")
        if draft.quote:
            print(f"  quote:  \"{draft.quote}\"")
        print(f"  options: {' / '.join(draft.options)}")
        print(f"  answer: {draft.answer or '(vote)'}   reveal: {draft.reveal_copy}")
        if draft.story_holder_id:
            holder = names[str(draft.story_holder_id)]
            line, by = await nudge_line(holder, list(names.values()), draft.prompt, draft.reveal_copy,
                                        ["NO WAY", "💀💀", "wait what"])
            print(f"  if the chat stalls: \"{line}\" ({by})")
        print()


if __name__ == "__main__":
    asyncio.run(main())
