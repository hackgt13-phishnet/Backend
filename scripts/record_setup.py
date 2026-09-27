"""Build a throwaway copy of the demo group under new profile ids, so recording a game never touches
a profile a teammate already claimed. Same real content, same real photos (Muse's cached reads), just
new ids. Writes its own rows to the database; never deletes or retires anyone else's moments.

  uv run --env-file .env --group ml python scripts/record_setup.py
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import add_photos
import seed_group
from build_memory import embed, item_text, to_items

from app.ai.memory import build_moments, cluster
from app.ai.naming import name_moment

ROOT = Path(__file__).resolve().parent.parent
RECORDING_NAMESPACE = uuid.uuid5(seed_group.NAMESPACE, "recording")
ITEMS_OUT = ROOT / "seed" / "recording_items.json"
MOMENTS_OUT = ROOT / "seed" / "recording_moments.json"


def build_items() -> dict:
    """The same seed_group content, under fresh ids: mutate its global NAMESPACE, since pid() and the
    item-id generator both read it from seed_group's own module globals, not a copy."""
    seed_group.NAMESPACE = RECORDING_NAMESPACE
    data = seed_group.build()
    # display_name is unique in the database, so these can't literally be named "Maya" again; the
    # suffix also keeps this run from ever being mistaken for the shared team demo in a live view.
    for p in data["profiles"]:
        p["display_name"] += " ·rec"
    return data


def attach_photos(data: dict) -> None:
    """Reuses Muse's already-cached reads (seed/photos.json) — no new Muse calls, same real text."""
    # attach() builds its own new-photo item ids from its own NAMESPACE global; it must move too.
    add_photos.NAMESPACE = RECORDING_NAMESPACE
    cached = {p["key"]: p for p in json.loads((ROOT / "seed" / "photos.json").read_text())}
    for source in json.loads((ROOT / "seed" / "photo_sources.json").read_text())["photos"]:
        photo = {**source, **cached[source["key"]]}
        add_photos.attach(data["items"], photo)


async def write_db(data: dict, rows: list[dict], vectors, moments) -> None:
    import asyncpg

    def vec(values) -> str:
        return "[" + ",".join(f"{v:.6f}" for v in values) + "]"

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction():
            for p in data["profiles"]:
                await conn.execute(
                    "INSERT INTO profiles(id, display_name) VALUES($1, $2) ON CONFLICT (id) DO NOTHING",
                    p["id"],
                    p["display_name"],
                )
            embedding_by_id = {row["id"]: vectors[n] for n, row in enumerate(rows)}
            for row in data["items"]:
                emb = embedding_by_id.get(row["id"])
                await conn.execute(
                    """INSERT INTO group_context_items(id, content_type, body, sender_profile_id,
                           participant_profile_ids, occurred_at, safe_for_demo, embedding,
                           media_url, media_description, media_credit)
                       VALUES($1, $2, $3, $4, $5::uuid[], $6, $7, $8::vector, $9, $10, $11)
                       ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding""",
                    row["id"],
                    row["content_type"],
                    row["body"],
                    row["sender_profile_id"],
                    row["participant_profile_ids"],
                    datetime.fromisoformat(row["occurred_at"]),
                    row["safe_for_demo"],
                    vec(emb) if emb is not None else None,
                    row.get("media_url"),
                    row.get("media_description"),
                    row.get("media_credit"),
                )
            for m in moments:
                await conn.execute(
                    """INSERT INTO moments(id, label, kind, keywords, item_ids, participant_profile_ids,
                           first_at, last_at, centroid)
                       VALUES($1, $2, $3, $4, $5::uuid[], $6::uuid[], $7, $8, $9::vector)
                       ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label""",
                    m.id,
                    m.label,
                    m.kind,
                    m.keywords,
                    m.item_ids,
                    m.participant_profile_ids,
                    m.first_at,
                    m.last_at,
                    vec(m.centroid),
                )
            # No delete/retire step here: this run must never touch moments from any other dataset.
    finally:
        await conn.close()


async def main() -> None:
    data = build_items()
    attach_photos(data)
    ITEMS_OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    rows = [row for row in data["items"] if row.get("safe_for_demo", True) and item_text(row)]
    vectors = embed([item_text(row) for row in rows])
    labels = cluster(vectors, min_cluster_size=5)
    moments = build_moments(to_items(rows), vectors, labels)
    by_id = {row["id"]: row for row in rows}
    for m in moments:
        m.label = await name_moment([item_text(by_id[i]) for i in m.item_ids], m.kind, m.keywords)
    MOMENTS_OUT.write_text(
        json.dumps(
            [{"id": m.id, "label": m.label, "kind": m.kind, "size": m.size} for m in moments],
            indent=1,
        )
        + "\n"
    )
    for m in moments:
        print(f"  [{m.kind:11}] {m.label:32} {m.size:3} items")

    await write_db(data, rows, vectors, moments)
    print(f"\nwrote {len(data['items'])} items, {len(moments)} moments under a fresh namespace")
    print(
        f"profiles: {json.dumps({p['display_name']: p['id'] for p in data['profiles']}, indent=1)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
