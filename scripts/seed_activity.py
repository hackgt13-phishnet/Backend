"""Generate each person's own Instagram activity: posts, stories, liked reels, saves, follows.

Posts and stories are public to followers. Likes, saves and follows are private: the AI may learn a
topic from them but must never quote them to other players.

Planted: Riya is new (no shared chat history with anyone) but overlaps with Dev (F1, rival teams),
Kofi (anime), Sam (horror) and Ana (pickleball). Sam and Ana clash on the NBA.

Run: uv run python scripts/seed_activity.py              # writes seed/activity.json
     uv run python scripts/seed_activity.py --write-db   # also loads it into Postgres (DATABASE_URL)
"""

import argparse
import asyncio
import json
import os
import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

NAMESPACE = uuid.UUID("6f1c2a52-8d0e-4b7a-9c3e-1f2a3b4c5d6e")
OUT = Path(__file__).resolve().parent.parent / "seed" / "activity.json"
PUBLIC = {"post", "story"}

# (kind, text)
ACTIVITY = {
    "Maya": [
        ("post", "first V4 send at stone summit 🧗‍♀️ took me 3 weeks"),
        ("post", "lisbon you will always have my heart 🇵🇹"),
        ("story", "mitski on repeat again. i'm fine"),
        ("liked_reel", "bouldering beta for crimpy overhangs"),
        ("liked_reel", "cheapest cities to visit in europe 2026"),
        ("saved", "pastel de nata recipe that actually works"),
        ("follow", "mitski (musician)"),
        ("follow", "magnus midtbø (climber)"),
    ],
    "Dev": [
        ("post", "ferrari strategy department needs to be investigated fr"),
        ("story", "5am race start and i'm UP. forza ferrari"),
        ("post", "new PR on bench 🏋️ 225 finally"),
        ("story", "gnx is kendrick's best album and it's not close"),
        ("liked_reel", "leclerc onboard monaco pole lap"),
        ("liked_reel", "mario kart rainbow road shortcuts"),
        ("saved", "push pull legs 6 day split"),
        ("follow", "scuderia ferrari"),
        ("follow", "kendrick lamar"),
    ],
    "Sam": [
        ("post", "lakers in 6. i will not be taking questions"),
        ("story", "watched hereditary alone at 2am. worst decision of my life"),
        ("post", "copped the travis 1s 👟 finally"),
        ("liked_reel", "lebron fadeaway compilation"),
        ("liked_reel", "a24 horror movies ranked"),
        ("saved", "best horror movies you haven't seen"),
        ("follow", "los angeles lakers"),
        ("follow", "a24"),
    ],
    "Ana": [
        ("post", "celtics own the east and y'all know it ☘️"),
        ("story", "pickleball at 7am with the girls, lost every game, still happy"),
        ("story", "eras tour movie for the 4th time. no regrets"),
        ("post", "iced lavender oat latte supremacy"),
        ("liked_reel", "jayson tatum clutch shots"),
        ("liked_reel", "pickleball dink drills for beginners"),
        ("saved", "taylor swift easter eggs explained"),
        ("follow", "boston celtics"),
        ("follow", "taylor swift"),
    ],
    "Kofi": [
        ("post", "made jollof in the rice cooker and it slapped 🍚"),
        ("story", "one piece episode 1100 had me in tears ngl"),
        ("post", "burna boy live was insane"),
        ("liked_reel", "rice cooker recipes for college students"),
        ("liked_reel", "gear 5 luffy animation breakdown"),
        ("saved", "jollof rice ratios"),
        ("follow", "burna boy"),
        ("follow", "one piece (anime)"),
    ],
    "Riya": [
        ("post", "papaya season 🧡 lando for the win"),
        ("story", "watching the race at 5am with coffee, mclaren or nothing"),
        ("post", "finished jujutsu kaisen and i need to talk about it with someone"),
        ("story", "talk to me hit different in theaters. new favorite horror"),
        ("post", "pickleball league starts sunday, looking for a partner"),
        ("liked_reel", "mclaren pit stop in 1.8 seconds"),
        ("liked_reel", "gojo vs sukuna breakdown"),
        ("saved", "best horror movies of the decade"),
        ("follow", "mclaren f1"),
        ("follow", "jujutsu kaisen (anime)"),
    ],
}


def build() -> dict:
    rng = random.Random(29)
    items = []
    for person, acts in ACTIVITY.items():
        owner = str(uuid.uuid5(NAMESPACE, f"profile:{person}"))
        for n, (kind, text) in enumerate(acts):
            when = datetime(2026, 9, 25, tzinfo=UTC) - timedelta(days=rng.randint(1, 120))
            items.append({
                "id": str(uuid.uuid5(NAMESPACE, f"activity:{person}:{n}")),
                "owner_profile_id": owner,
                "kind": kind,
                "visibility": "public" if kind in PUBLIC else "private",
                "text": text,
                "occurred_at": when.isoformat(),
            })
    profiles = [{"id": str(uuid.uuid5(NAMESPACE, f"profile:{p}")), "display_name": p} for p in ACTIVITY]
    return {"profiles": profiles, "activity": items}


async def write_db(data: dict) -> None:
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction():
            for p in data["profiles"]:
                await conn.execute(
                    "INSERT INTO profiles(id, display_name) VALUES($1, $2) ON CONFLICT (id) DO NOTHING",
                    p["id"], p["display_name"],
                )
            for a in data["activity"]:
                await conn.execute(
                    """INSERT INTO player_activity(id, owner_profile_id, kind, visibility, text, occurred_at)
                       VALUES($1, $2, $3, $4, $5, $6) ON CONFLICT (id) DO NOTHING""",
                    a["id"], a["owner_profile_id"], a["kind"], a["visibility"], a["text"],
                    datetime.fromisoformat(a["occurred_at"]),
                )
    finally:
        await conn.close()
    print(f"loaded {len(data['activity'])} activity items into the database")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write-db", action="store_true")
    args = parser.parse_args()
    data = build()
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(data['activity'])} activity items for {len(data['profiles'])} people to {OUT}")
    if args.write_db:
        asyncio.run(write_db(data))


if __name__ == "__main__":
    main()
