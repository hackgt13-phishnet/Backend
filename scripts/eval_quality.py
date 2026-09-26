"""Score interest rounds (on topic?) and nudges (worth answering?) with a separate Muse judge.

Interests and links are cached on the first run so before/after runs score the same inputs.

  uv run --env-file .env python scripts/eval_quality.py --out before.json
  uv run --env-file .env python scripts/eval_quality.py --out after.json --compare before.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai import host, rounds
from app.ai.interests import ActivityItem, Interest, Link, extract_interests, find_links
from app.ai.llm import complete_json
from app.domain import GameType

SEED = Path(__file__).resolve().parent.parent / "seed"
ROOMS = [["Dev", "Kofi", "Riya"], ["Maya", "Dev", "Sam", "Ana", "Kofi"]]
SAMPLES = 5

ROUND_JUDGE = (
    "You check party-game rounds. Given the specific interests of the players a round is built on, "
    "decide if the round is clearly about THOSE specifics (their actual teams, shows, artists, activities). "
    "It fails if it brings in other franchises/teams/artists the players never mentioned, or is so generic "
    'it could be about anyone. JSON: {"on_topic": true or false, "why": "..."}'
)
NUDGE_JUDGE = (
    "You check one line a game host sends to a quiet group chat after a round. Pass it only if it gives "
    "the named person a specific, easy reason to talk (their own pick, their story, why the group picked "
    "them), does NOT just repeat the reveal, and does NOT ask them to defend a statement they never made. "
    'JSON: {"good": true or false, "why": "..."}'
)


async def cached_inputs(path: Path) -> tuple[dict, list[dict]]:
    if path.exists():
        return json.loads(path.read_text()), []
    data = json.loads((SEED / "activity.json").read_text())
    names = {p["id"]: p["display_name"] for p in data["profiles"]}
    per = {n: [ActivityItem(a["id"], a["kind"], a["visibility"], a["text"]) for a in data["activity"]
               if a["owner_profile_id"] == pid] for pid, n in names.items()}
    extracted = dict(zip(per, await asyncio.gather(*(extract_interests(v) for v in per.values())), strict=True))
    rooms = []
    for room in ROOMS:
        interests = {n: extracted[n] for n in room}
        links = await find_links(interests)
        rooms.append({"room": room, "links": [l.__dict__ for l in links]})
    blob = {"interests": {n: [i.__dict__ for i in v] for n, v in extracted.items()}, "rooms": rooms}
    path.write_text(json.dumps(blob, indent=1))
    return blob, []


def rebuild(blob: dict):
    interests = {n: [Interest(i["topic"], i["detail"], tuple(i["evidence"]), i["public"]) for i in v]
                 for n, v in blob["interests"].items()}
    return interests, [(r["room"], [Link(l["kind"], l["topic"], l["angle"], l["players"]) for l in r["links"]])
                       for r in blob["rooms"]]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--cache", type=Path, default=Path("/tmp/eval_inputs.json"))
    args = parser.parse_args()

    blob, _ = await cached_inputs(args.cache)
    interests, room_links = rebuild(blob)
    results = {"rounds": [], "nudges": []}

    for room, links in room_links:
        names = {f"id-{n}": n for n in room}
        name_to_id = {v: k for k, v in names.items()}
        uuid_ids = {n: f"00000000-0000-0000-0000-{i:012d}" for i, n in enumerate(room)}
        for link in links:
            specifics = {n: interests[n][i].shareable() for n, i in link.players.items()}
            writer = rounds.hot_take if link.kind == "clash" else rounds.this_or_that
            for _ in range(SAMPLES):
                draft = await rounds_call(writer, link, names, uuid_ids, interests)
                text = f"{draft.prompt} [{' / '.join(draft.options)}]"
                verdict = await complete_json(ROUND_JUDGE, json.dumps({"interests": specifics, "round": text}))
                results["rounds"].append({"room": room, "topic": link.topic, "round": text, "by": draft.written_by,
                                          "on_topic": bool((verdict or {}).get("on_topic")),
                                          "why": (verdict or {}).get("why")})

                # A realistic reveal for this round: everyone but one person picked the first option.
                votes = {n: draft.options[0] for n in room}
                lone = list(link.players)[-1]
                votes[lone] = draft.options[1]
                line, by = await nudge_call(lone, room, draft, votes)
                verdict = await complete_json(NUDGE_JUDGE, json.dumps(
                    {"round": text, "votes": votes, "reveal": draft.reveal_copy, "line": line, "person": lone}))
                results["nudges"].append({"round": text, "person": lone, "line": line, "by": by,
                                          "good": bool((verdict or {}).get("good")), "why": (verdict or {}).get("why")})

    args.out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    on = sum(r["on_topic"] for r in results["rounds"]) / len(results["rounds"])
    good = sum(n["good"] for n in results["nudges"]) / len(results["nudges"])
    print(f"rounds on topic: {on:.0%} of {len(results['rounds'])} · nudges worth answering: {good:.0%} of {len(results['nudges'])}")
    if args.compare:
        before = json.loads(args.compare.read_text())
        b_on = sum(r["on_topic"] for r in before["rounds"]) / len(before["rounds"])
        b_good = sum(n["good"] for n in before["nudges"]) / len(before["nudges"])
        print(f"before:          {b_on:.0%}                         {b_good:.0%}")


async def rounds_call(writer, link, names, uuid_ids, interests):
    try:  # new signature passes the players' specifics
        return await writer(link, names, uuid_ids, interests)
    except TypeError:
        return await writer(link, names, uuid_ids)


async def nudge_call(person, room, draft, votes):
    try:  # new signature takes the round type and the person's own pick
        return await host.nudge_line(person, room, draft.prompt, draft.reveal_copy, [],
                                     game_type=draft.game_type, their_pick=votes[person], votes=votes)
    except TypeError:
        return await host.nudge_line(person, room, draft.prompt, draft.reveal_copy, [])


if __name__ == "__main__":
    _ = GameType
    asyncio.run(main())
