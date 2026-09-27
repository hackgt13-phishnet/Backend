"""Add the demo photos: fetch them from Wikimedia Commons, have Muse read each one, attach them to the seed.

Muse's reads are cached in seed/photos.json, so re-running only calls Muse for new photos (or --refresh).

  uv run --env-file .env python scripts/add_photos.py
  uv run --group ml python scripts/build_memory.py --write-db     # then rebuild the group's memory
"""

import argparse
import asyncio
import html
import json
import re
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_group import NAMESPACE, PEOPLE, pid

from app.ai.vision import read_photo

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "seed" / "photo_sources.json"
PHOTOS = ROOT / "seed" / "photos.json"
ITEMS = ROOT / "seed" / "group_items.json"
CACHE = ROOT / "data" / "media"
COMMONS = "https://commons.wikimedia.org/w/api.php"
HEADERS = {"User-Agent": "hackgt13-instagram-games/0.1 (demo seed data)"}
KINDS = {
    "lisbon_trip": "moment",
    "fire_alarm": "moment",
    "finals_week": "moment",
    "rice_cooker": "inside_joke",
    "mario_kart": "inside_joke",
}


def plain(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


async def get(client: httpx.AsyncClient, url: str, **params) -> httpx.Response:
    """GET with backoff: Commons rate-limits bursts."""
    for wait in (2, 5, 10, 20, 40):
        r = await client.get(url, params=params or None)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        await asyncio.sleep(wait)
    r.raise_for_status()
    return r


async def commons_info(client: httpx.AsyncClient, titles: list[str]) -> dict[str, dict]:
    """Image URL, credit and licence for every title, in one request."""
    r = await get(
        client,
        COMMONS,
        action="query",
        format="json",
        titles="|".join(titles),
        prop="imageinfo",
        iiprop="url|extmetadata",
        iiurlwidth=1024,
    )
    body = r.json()["query"]
    rename = {n["to"]: n["from"] for n in body.get("normalized", [])}
    out = {}
    for page in body["pages"].values():
        info = page["imageinfo"][0]
        meta = info["extmetadata"]
        out[rename.get(page["title"], page["title"])] = {
            "url": info["thumburl"],
            "page": info["descriptionurl"],
            "credit": plain(meta.get("Artist", {}).get("value", "unknown")),
            "license": meta.get("LicenseShortName", {}).get("value", ""),
            "commons_description": plain(meta.get("ImageDescription", {}).get("value", ""))[:300],
        }
    return out


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> bytes:
    path = CACHE / f"{key}.jpg"
    if not path.exists():
        path.write_bytes((await get(client, url)).content)
        await asyncio.sleep(1)
    return path.read_bytes()


def attach(items: list[dict], photo: dict) -> str:
    """Put the photo on its message, or add it as a new caption-less photo. Returns the item id."""
    read = photo["muse"]
    fields = {
        "media_url": photo["url"],
        "media_description": read["description"],
        "media_credit": f"{photo['credit']} · {photo['license']} · Wikimedia Commons",
    }
    if caption := photo.get("caption"):
        item = next(i for i in items if i["body"] == caption and i["content_type"] == "photo")
        item.update(fields)
        # Safe only if both the caption and the photo are; remember the caption's own verdict so re-reads can't stick.
        item.setdefault("caption_safe_for_demo", item["safe_for_demo"])
        item["safe_for_demo"] = item["caption_safe_for_demo"] and read["safe"]
        return item["id"]
    new = photo["new"]
    people = PEOPLE if new["participants"] == "everyone" else new["participants"]
    item_id = str(uuid.uuid5(NAMESPACE, f"photo:{photo['key']}"))
    items[:] = [i for i in items if i["id"] != item_id]
    items.append(
        {
            "id": item_id,
            "content_type": "photo",
            "body": None,
            **fields,
            "sender_profile_id": pid(new["sender"]),
            "participant_profile_ids": sorted(pid(p) for p in set(people) | {new["sender"]}),
            "occurred_at": new["at"],
            "safe_for_demo": read["safe"],
            "planted_theme": photo["theme"],
            "planted_kind": KINDS.get(photo["theme"]),
        }
    )
    return item_id


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh", action="store_true", help="ask Muse again even for photos already read"
    )
    args = parser.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    sources = json.loads(SOURCES.read_text())["photos"]
    done = {p["key"]: p for p in json.loads(PHOTOS.read_text())} if PHOTOS.exists() else {}
    data = json.loads(ITEMS.read_text())

    photos = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        info = await commons_info(client, [s["commons"] for s in sources])
        for source in sources:
            photo = {**source, **info[source["commons"]]}
            image = await fetch(client, source["key"], photo["url"])
            cached = done.get(source["key"], {}).get("muse")
            if cached and not args.refresh:
                photo["muse"] = cached
            else:
                read = await read_photo(image)
                if read is None:
                    sys.exit(f"Muse couldn't read {source['key']}; nothing written. Try again.")
                photo["muse"] = {
                    "description": read.description,
                    "safe": read.safe,
                    "reason": read.reason,
                }
            photo["item_id"] = attach(data["items"], photo)
            flag = "" if photo["muse"]["safe"] else f"   ✗ blocked: {photo['muse']['reason']}"
            print(f"  {source['key']:20} {photo['muse']['description']}{flag}")
            photos.append(photo)

    data["items"].sort(key=lambda i: i["occurred_at"])
    ITEMS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    PHOTOS.write_text(json.dumps(photos, indent=1, ensure_ascii=False) + "\n")
    print(
        f"\n{len(photos)} photos, {sum(not p['muse']['safe'] for p in photos)} blocked → {ITEMS.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
