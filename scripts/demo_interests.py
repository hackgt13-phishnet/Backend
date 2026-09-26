"""Play the planner offline for a few rooms: moments from shared history, interests from each
player's own activity, links between players, then Muse writes the rounds.

  uv run --env-file .env python scripts/demo_interests.py
"""

import asyncio
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.interests import ActivityItem, extract_interests, find_links
from app.ai.llm import models_from_env
from app.ai.picker import ItemView, MomentView, member_vectors_from_items, score_moments
from app.ai.planner import plan_session
from app.domain import ROUNDS_PER_SESSION

SEED = Path(__file__).resolve().parent.parent / "seed"
ROOMS = [
    ["Dev", "Riya"],
    ["Maya", "Dev", "Sam", "Ana", "Kofi"],
    ["Maya", "Dev", "Sam", "Ana", "Kofi", "Riya"],
]


async def main() -> None:
    group = json.loads((SEED / "group_items.json").read_text())
    activity = json.loads((SEED / "activity.json").read_text())
    moments_json = json.loads((SEED / "moments.json").read_text())
    emb = np.load(SEED / "item_embeddings.npz")
    all_names = {p["id"]: p["display_name"] for p in activity["profiles"]}
    items = {
        r["id"]: ItemView(
            r["id"], r["sender_profile_id"], frozenset(r["participant_profile_ids"]), r["body"]
        )
        for r in group["items"]
        if r["safe_for_demo"]
    }
    vectors = member_vectors_from_items(
        dict(zip(emb["ids"].tolist(), emb["vectors"], strict=True)), items
    )
    moments = [
        MomentView(
            m["id"],
            m["kind"],
            tuple(m["item_ids"]),
            frozenset(m["participant_profile_ids"]),
            np.array(m["centroid"]),
        )
        for m in moments_json
    ]
    labels = {m["id"]: m["label"] for m in moments_json}
    print(
        f"writer: {', '.join(m.model for m in models_from_env()) or 'templates only (no LLM key set)'}"
    )

    # Interests are per person, so extract them once and reuse across rooms.
    per_person = {}
    for pid, name in all_names.items():
        acts = [
            ActivityItem(a["id"], a["kind"], a["visibility"], a["text"])
            for a in activity["activity"]
            if a["owner_profile_id"] == pid
        ]
        per_person[name] = acts
    extracted = dict(
        zip(
            per_person,
            await asyncio.gather(*(extract_interests(a) for a in per_person.values())),
            strict=True,
        )
    )

    for room in ROOMS:
        names = {pid: n for pid, n in all_names.items() if n in room}
        picks = score_moments(moments, items, frozenset(names), vectors)
        interests = {n: extracted[n] for n in room}
        links = await find_links(interests)
        branch, drafts = await plan_session(picks, links, interests, names, ROUNDS_PER_SESSION)
        print(
            f"\n━━ room: {', '.join(room)} · {len(picks)} playable moments · {len(links)} links → branch: {branch}"
        )
        for name in room:
            print(
                f"   {name:5} into: "
                + "; ".join(
                    i.shareable() + ("" if i.public else " (private)") for i in interests[name]
                )
            )
        for link in links:
            print(f"   {link.kind:7} {link.topic}: {', '.join(link.players)} · {link.angle}")
        for n, d in enumerate(drafts, 1):
            where = labels.get(str(d.moment_id), d.source) if d.moment_id else d.source
            print(f"   round {n} · {d.game_type.value} · from {where} · {d.written_by}")
            print(
                f"      {d.prompt}"
                + (f'  "{d.quote}"' if d.quote else "")
                + f"  [{' / '.join(d.options)}]"
            )
            print(f"      reveal: {d.reveal_copy}")


if __name__ == "__main__":
    asyncio.run(main())
