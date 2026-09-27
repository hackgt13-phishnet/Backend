"""Give the seeded friends real-looking photo posts from Unsplash, have Muse read each photo (setting,
activity, how many people; never who), and load them.

A post that tags friends also becomes shared history with them (they were there), so the memory step
can find events people were at together even if nobody messaged about it.

  uv run --env-file .env python scripts/add_post_photos.py              # fetch + read + write seeds
  uv run --env-file .env python scripts/add_post_photos.py --write-db   # also load posts into Postgres
  uv run --env-file .env --group ml python scripts/build_memory.py --write-db   # then rebuild moments

Needs UNSPLASH_ACCESS_KEY in .env. Muse's reads are cached in seed/post_photos.json (--refresh to redo).
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_group import NAMESPACE, pid

from app.ai.vision import read_post

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "seed" / "post_sources.json"
READS = ROOT / "seed" / "post_photos.json"
ITEMS = ROOT / "seed" / "group_items.json"
CACHE = ROOT / "data" / "media" / "posts"
API = "https://api.unsplash.com"


async def unsplash_photo(client: httpx.AsyncClient, post: dict) -> dict:
    """The chosen search result, cached so re-runs don't spend the 50/hour API quota."""
    cached = CACHE / f"search_{post['key']}.json"
    if not cached.exists():
        r = await client.get(
            f"{API}/search/photos", params={"query": post["query"], "per_page": 10}
        )
        r.raise_for_status()
        cached.write_text(r.text)
    photo = json.loads(cached.read_text())["results"][post.get("pick", 0)]
    return {
        "unsplash_id": photo["id"],
        "url": photo["urls"]["regular"],  # Unsplash asks apps to hotlink their image URLs
        "small": photo["urls"]["small"],
        "download_location": photo["links"]["download_location"],
        "credit": f"{photo['user']['name']} · Unsplash",
        "page": photo["links"]["html"],
    }


async def image_bytes(client: httpx.AsyncClient, post: dict, photo: dict) -> bytes:
    path = CACHE / f"{post['key']}.jpg"
    if not path.exists():
        await client.get(photo["download_location"])  # Unsplash API guideline: count the use
        r = await client.get(photo["small"])
        r.raise_for_status()
        path.write_bytes(r.content)
    return path.read_bytes()


def activity_id(post: dict) -> str:
    return str(uuid.uuid5(NAMESPACE, f"postphoto:{post['key']}"))


def as_group_item(post: dict) -> dict:
    """A tagged post is shared history between the poster and everyone tagged."""
    read = post["muse"]
    seen = read["summary"] + (f" at {post['location']}" if post.get("location") else "")
    return {
        "id": str(uuid.uuid5(NAMESPACE, f"taggedpost:{post['key']}")),
        "content_type": "post",
        "body": post["caption"] or None,
        "media_url": post["url"],
        "media_description": seen,
        "media_credit": post["credit"],
        "shows_person": read["people"] > 0,
        "sender_profile_id": pid(post["owner"]),
        "participant_profile_ids": sorted(pid(p) for p in {post["owner"], *post["tagged"]}),
        "occurred_at": post["at"],
        "safe_for_demo": read["safe"],
        "planted_theme": post["theme"],
        "planted_kind": "moment" if post["theme"] == "beach_day" else None,
    }


async def write_db(posts: list[dict]) -> None:
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction():
            for p in posts:
                read = p["muse"]
                if not read["safe"]:  # never stored where a round could use it
                    await conn.execute("DELETE FROM player_activity WHERE id = $1", activity_id(p))
                    continue
                await conn.execute(
                    """INSERT INTO player_activity(id, owner_profile_id, kind, visibility, text, occurred_at,
                           media_url, media_credit, media_read, location, tagged_profile_ids)
                       VALUES($1, $2, $3, 'public', $4, $5, $6, $7, $8::jsonb, $9, $10::uuid[])
                       ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, media_url = EXCLUDED.media_url,
                           media_credit = EXCLUDED.media_credit, media_read = EXCLUDED.media_read,
                           location = EXCLUDED.location, tagged_profile_ids = EXCLUDED.tagged_profile_ids""",
                    activity_id(p),
                    pid(p["owner"]),
                    p["kind"],
                    p["caption"],
                    datetime.fromisoformat(p["at"]),
                    p["url"],
                    p["credit"],
                    json.dumps(read),
                    p["location"] or None,
                    [pid(t) for t in p["tagged"]],
                )
    finally:
        await conn.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh", action="store_true", help="ask Muse again even if already read"
    )
    parser.add_argument("--write-db", action="store_true")
    args = parser.parse_args()
    key = os.environ.get("UNSPLASH_ACCESS_KEY")
    if not key:
        sys.exit(
            "UNSPLASH_ACCESS_KEY is missing from .env (unsplash.com/developers → New Application)."
        )

    CACHE.mkdir(parents=True, exist_ok=True)
    sources = json.loads(SOURCES.read_text())["posts"]
    done = {p["key"]: p for p in json.loads(READS.read_text())} if READS.exists() else {}
    posts = []
    async with httpx.AsyncClient(
        headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"}, timeout=30
    ) as client:
        for source in sources:
            photo = await unsplash_photo(client, source)
            post = {**source, **photo}
            cached = done.get(source["key"], {})
            if (
                cached.get("muse")
                and cached.get("unsplash_id") == photo["unsplash_id"]
                and not args.refresh
            ):
                post["muse"] = cached["muse"]
            else:
                read = await read_post(await image_bytes(client, source, photo), source["caption"])
                if read is None:
                    sys.exit(f"Muse couldn't read {source['key']}; nothing written. Try again.")
                post["muse"] = {
                    "place": read.place,
                    "activity": read.activity,
                    "people": read.people,
                    "summary": read.summary,
                    "safe": read.safe,
                    "reason": read.reason,
                }
            m = post["muse"]
            flag = "" if m["safe"] else f"  ✗ blocked: {m['reason']}"
            print(
                f"  {source['key']:18} people={m['people']} place={m['place']!r} activity={m['activity']!r}{flag}"
            )
            posts.append(post)

    READS.write_text(json.dumps(posts, indent=1, ensure_ascii=False) + "\n")
    data = json.loads(ITEMS.read_text())
    tagged = [as_group_item(p) for p in posts if p["tagged"]]
    ids = {t["id"] for t in tagged}
    data["items"] = [i for i in data["items"] if i["id"] not in ids] + tagged
    data["items"].sort(key=lambda i: i["occurred_at"])
    ITEMS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"\n{len(posts)} photo posts, {len(tagged)} tagged → shared history")
    if args.write_db:
        await write_db(posts)
        print("loaded into player_activity")


if __name__ == "__main__":
    asyncio.run(main())
