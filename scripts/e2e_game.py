"""Play a real game against a running backend + Supabase, the way the app would.

  uv run --env-file .env uvicorn app.main:app --port 8000      # in another terminal
  uv run --env-file .env python scripts/e2e_game.py Dev Kofi Riya

Players sign in anonymously, pick a seeded profile, join one room, the first one starts a game,
everyone answers, and the chat reacts. Prints the room's timeline as it happens.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

API = os.environ.get("API_URL", "http://127.0.0.1:8000/v1")
SEED = Path(__file__).resolve().parent.parent / "seed"
CHAT = ["NO WAY", "wait who said that 💀", "i knew it", "lmaooo", "ok that's actually so funny"]


async def sign_in(client: httpx.AsyncClient) -> str:
    r = await client.post(
        f"{os.environ['SUPABASE_URL']}/auth/v1/signup",
        headers={"apikey": os.environ["SUPABASE_ANON_KEY"]},
        json={},
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def main(names: list[str]) -> None:
    profiles = {
        p["display_name"]: p["id"]
        for p in json.loads((SEED / "activity.json").read_text())["profiles"]
    }
    async with httpx.AsyncClient(timeout=60) as c:
        headers = {}
        for name in names:
            token = await sign_in(c)
            headers[name] = {"Authorization": f"Bearer {token}"}
            (
                await c.post(
                    f"{API}/demo-sessions",
                    headers=headers[name],
                    json={"profile_id": profiles[name]},
                )
            ).raise_for_status()
        host = names[0]
        room = (
            await c.post(f"{API}/rooms", headers=headers[host], json={"name": "e2e test"})
        ).json()
        for name in names[1:]:
            (
                await c.post(
                    f"{API}/rooms/join", headers=headers[name], json={"code": room["join_code"]}
                )
            ).raise_for_status()
        print(f"room {room['join_code']} · players: {', '.join(names)}")

        start = time.perf_counter()
        r = await c.post(
            f"{API}/rooms/{room['id']}/sessions", headers=headers[host], json={"vibe": "chaos"}
        )
        print(
            f"start game → {r.status_code} in {time.perf_counter() - start:.1f}s {'' if r.is_success else r.text}"
        )
        if not r.is_success:
            return

        seen, answered, chatted, deadline = set(), set(), set(), time.time() + 420
        while time.time() < deadline:
            r = await c.get(f"{API}/rooms/{room['id']}/timeline", headers=headers[host])
            if not r.is_success:
                print(f"    (timeline request failed: {r.status_code}, retrying)")
                await asyncio.sleep(2)
                continue
            events = r.json()["events"]
            for e in events:
                if e["id"] in seen:
                    continue
                seen.add(e["id"])
                p, t = e["payload"], e["event_type"]
                if t == "game_started":
                    print(f"  ▶ game started · branch: {p.get('branch')}")
                elif t == "game_prompt":
                    q = f' "{p["quote"]}"' if p.get("quote") else ""
                    print(
                        f"  ? round {p['ordinal']} · {p['game_type']}: {p['prompt']}{q}  [{' / '.join(p['options'])}]"
                    )
                elif t == "submission_status":
                    print(f"    answered {p['answered']}/{p['total']}")
                elif t == "game_reveal":
                    if p.get("game_over"):
                        print(
                            f"  ■ game over · {time.perf_counter() - start:.0f}s from tapping start"
                        )
                        deadline = 0
                    else:
                        print(
                            f'  ! reveal · answer: {p.get("answer")} · votes: {p.get("votes")} · "{p.get("reveal")}"'
                        )
                elif t == "host_line":
                    print(f'  ✦ game master: "{p["text"]}" ({p.get("written_by")})')
                elif t == "message":
                    pass
            # Answer any open round once per player; chat a bit after each reveal.
            for e in events:
                p = e["payload"]
                if e["event_type"] == "game_prompt" and p["round_id"] not in answered:
                    answered.add(p["round_id"])
                    for i, name in enumerate(names):
                        await c.post(
                            f"{API}/rounds/{p['round_id']}/responses",
                            headers=headers[name],
                            json={"value": p["options"][i % len(p["options"])]},
                        )
                if (
                    e["event_type"] == "game_reveal"
                    and e["id"] not in chatted
                    and not p.get("game_over")
                ):
                    chatted.add(e["id"])
                    for i, name in enumerate(names):
                        await c.post(
                            f"{API}/rooms/{room['id']}/messages",
                            headers=headers[name],
                            json={"body": CHAT[i % len(CHAT)]},
                        )
                        await asyncio.sleep(3)
            await asyncio.sleep(3)

        decisions = (
            await c.get(f"{API}/rooms/{room['id']}/gm-decisions", headers=headers[host])
        ).json()
        acted = [d for d in reversed(decisions) if d["action"] != "wait"]
        print(
            f"\ngame master: {len(decisions)} decisions, {len(decisions) - len(acted)} were 'wait'"
        )
        for d in acted:
            p = f"{d['p_silence']:.2f}" if d["p_silence"] is not None else " –  "
            print(f"  {d['action']:10} P(quiet) {p} · {d['reason']} · {d['model_source']}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["Dev", "Kofi", "Riya"]))
