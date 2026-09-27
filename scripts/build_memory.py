"""Build a group's memory: embed its content, cluster it into moments, name them.

Offline step. Loads the embedding model here so the API server never has to.

  uv run --group ml python scripts/build_memory.py                  # seed data → seed/moments.json
  uv run --group ml python scripts/build_memory.py --write-db       # also write to Postgres (DATABASE_URL)
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.memory import Item, Moment, build_moments, cluster
from app.ai.naming import name_moment

ROOT = Path(__file__).resolve().parent.parent
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def item_text(row: dict) -> str:
    """What a photo means is its caption plus what Muse saw in it; a photo with no caption is just what Muse saw."""
    body, seen = row.get("body"), row.get("media_description")
    if body and seen:
        return f"{body} (photo: {seen})"
    return body or seen or ""


def load_items(path: Path) -> tuple[dict, list[dict]]:
    data = json.loads(path.read_text())
    safe = [row for row in data["items"] if row.get("safe_for_demo", True) and item_text(row)]
    return data, safe


def embed(texts: list[str]) -> np.ndarray:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBEDDING_MODEL)
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


def to_items(rows: list[dict]) -> list[Item]:
    return [
        Item(
            id=row["id"],
            body=item_text(row),
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            sender_profile_id=row["sender_profile_id"],
            participant_profile_ids=tuple(row["participant_profile_ids"]),
        )
        for row in rows
    ]


def report(moments: list[Moment], rows: list[dict], labels: np.ndarray) -> None:
    by_id = {row["id"]: row for row in rows}
    print(
        f"\n{len(rows)} safe items → {len(moments)} moments, {int((labels == -1).sum())} left as noise\n"
    )
    for m in moments:
        themes = Counter(by_id[i].get("planted_theme") for i in m.item_ids)
        theme, hits = themes.most_common(1)[0]
        print(
            f"  [{m.kind:11}] {m.label:32} {m.size:3} items  "
            f"{m.first_at:%b %d %Y} → {m.last_at:%b %d %Y}  "
            f"planted: {theme} ({hits}/{m.size})"
        )

    planted = Counter(row.get("planted_theme") for row in rows if row.get("planted_kind"))
    if planted:
        print("\n  recall per planted theme:")
        for theme, total in sorted(planted.items()):
            best = max(
                (sum(by_id[i].get("planted_theme") == theme for i in m.item_ids) for m in moments),
                default=0,
            )
            print(f"    {theme:12} {best}/{total}")


async def write_db(
    data: dict, rows: list[dict], vectors: np.ndarray, moments: list[Moment]
) -> None:
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
                           media_url, media_description, media_credit, shows_person)
                       VALUES($1, $2, $3, $4, $5::uuid[], $6, $7, $8::vector, $9, $10, $11, $12)
                       ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding,
                           body = EXCLUDED.body, safe_for_demo = EXCLUDED.safe_for_demo,
                           media_url = EXCLUDED.media_url, media_description = EXCLUDED.media_description,
                           media_credit = EXCLUDED.media_credit, shows_person = EXCLUDED.shows_person""",
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
                    bool(row.get("shows_person")),
                )
            for m in moments:
                await conn.execute(
                    """INSERT INTO moments(id, label, kind, keywords, item_ids, participant_profile_ids,
                           first_at, last_at, centroid)
                       VALUES($1, $2, $3, $4, $5::uuid[], $6::uuid[], $7, $8, $9::vector)
                       ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, kind = EXCLUDED.kind,
                           keywords = EXCLUDED.keywords, centroid = EXCLUDED.centroid""",
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
            # Drop moments that no longer exist. Ones a played round still points to are retired instead.
            # Only moments built from this dataset's items: other datasets (e.g. the X-ray recording
            # copy) share the table and must never be deleted or retired by this run.
            current = [m.id for m in moments]
            ours = [row["id"] for row in data["items"]]
            await conn.execute(
                """DELETE FROM moments WHERE NOT (id = ANY($1::uuid[])) AND item_ids && $2::uuid[]
                   AND id NOT IN (SELECT moment_id FROM rounds WHERE moment_id IS NOT NULL)""",
                current,
                ours,
            )
            await conn.execute(
                """UPDATE moments SET retired_at = CASE WHEN id = ANY($1::uuid[]) THEN NULL
                                                       ELSE coalesce(retired_at, now()) END
                   WHERE id = ANY($1::uuid[]) OR item_ids && $2::uuid[]""",
                current,
                ours,
            )
    finally:
        await conn.close()
    print(f"\nwrote {len(data['items'])} items and {len(moments)} moments to the database")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--items", type=Path, default=ROOT / "seed" / "group_items.json")
    parser.add_argument("--out", type=Path, default=ROOT / "seed" / "moments.json")
    parser.add_argument("--min-cluster-size", type=int, default=5)
    parser.add_argument("--write-db", action="store_true")
    args = parser.parse_args()

    data, rows = load_items(args.items)
    vectors = embed([item_text(row) for row in rows])
    labels = cluster(vectors, min_cluster_size=args.min_cluster_size)
    items = to_items(rows)
    moments = build_moments(items, vectors, labels)

    by_id = {row["id"]: row for row in rows}
    for m in moments:
        m.label = await name_moment([item_text(by_id[i]) for i in m.item_ids], m.kind, m.keywords)

    report(moments, rows, labels)
    args.out.write_text(
        json.dumps(
            [
                {
                    "id": m.id,
                    "label": m.label,
                    "kind": m.kind,
                    "keywords": m.keywords,
                    "item_ids": m.item_ids,
                    "participant_profile_ids": m.participant_profile_ids,
                    "first_at": m.first_at.isoformat(),
                    "last_at": m.last_at.isoformat(),
                    "size": m.size,
                    "centroid": m.centroid,
                }
                for m in moments
            ],
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    np.savez_compressed(
        args.out.with_name("item_embeddings.npz"),
        ids=np.array([row["id"] for row in rows]),
        vectors=vectors.astype(np.float32),
    )
    print(f"\nwrote {args.out.relative_to(ROOT)} and item_embeddings.npz")

    if args.write_db:
        await write_db(data, rows, vectors, moments)


if __name__ == "__main__":
    asyncio.run(main())
