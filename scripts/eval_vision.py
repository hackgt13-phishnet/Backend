"""Does reading photos help the group's memory? Runs the real clustering three ways and checks where each
photo lands:

  caption only       what we had before: a photo is only its caption (no caption = invisible)
  caption + Muse     the pipeline now: caption plus Muse's one-line read of the photo
  caption + Commons  reference only: the human-written description from Wikimedia Commons

Also reports the safety check against the photos' should_be_safe labels.

  uv run --group ml python scripts/eval_vision.py
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_memory import embed

from app.ai.memory import cluster

ROOT = Path(__file__).resolve().parent.parent


def placements(rows: list[dict], texts: list[str]) -> dict[str, str]:
    """item id → the planted theme of the moment it landed in ('none' if it's left as noise)."""
    keep = [(r, t) for r, t in zip(rows, texts, strict=True) if t]
    labels = cluster(embed([t for _, t in keep]), min_cluster_size=5)
    theme_of = {}
    for label in set(labels) - {-1}:
        members = [r for (r, _), lab in zip(keep, labels, strict=True) if lab == label]
        theme_of[label] = Counter(
            r["planted_theme"] for r in members if r["content_type"] == "message"
        ).most_common(1)[0][0]
    placed = {r["id"]: theme_of.get(lab, "none") for (r, _), lab in zip(keep, labels, strict=True)}
    return {r["id"]: placed.get(r["id"], "invisible") for r in rows}


def main() -> None:
    items = json.loads((ROOT / "seed" / "group_items.json").read_text())["items"]
    photos = json.loads((ROOT / "seed" / "photos.json").read_text())
    by_item = {p["item_id"]: p for p in photos}
    rows = [r for r in items if r["safe_for_demo"]]
    shown = [by_item[r["id"]] for r in rows if r["id"] in by_item]

    def texts(photo_text):
        return [
            r["body"] if r["id"] not in by_item else photo_text(r, by_item[r["id"]]) for r in rows
        ]

    def join(caption, seen):
        return f"{caption} (photo: {seen})" if caption else seen

    variants = {
        "caption only": texts(lambda r, p: r["body"]),
        "caption + Muse": texts(lambda r, p: join(r["body"], p["muse"]["description"])),
        "caption + Commons": texts(lambda r, p: join(r["body"], p["commons_description"])),
    }
    expected = {p["item_id"]: ("none" if p["theme"] == "noise" else p["theme"]) for p in shown}

    print(
        f"{len(shown)} photos shown to the group ({sum(1 for p in shown if not p.get('caption'))} with no caption)\n"
    )
    header = f"  {'photo':22}{'should land in':16}" + "".join(f"{v:20}" for v in variants)
    print(header)
    results = {name: placements(rows, t) for name, t in variants.items()}
    for p in shown:
        cells = "".join(
            f"{('✓ ' if results[v][p['item_id']] == expected[p['item_id']] else '✗ ') + results[v][p['item_id']]:20}"
            for v in variants
        )
        print(f"  {p['key']:22}{expected[p['item_id']]:16}{cells}")
    print(
        "\n  right place:      "
        + "".join(
            f"{sum(results[v][i] == e for i, e in expected.items())}/{len(expected):<18}"
            for v in variants
        )
    )
    no_caption = [i for i in expected if not by_item[i].get("caption")]
    print(
        "  no-caption only:  "
        + "".join(
            f"{sum(results[v][i] == expected[i] for i in no_caption)}/{len(no_caption):<18}"
            for v in variants
        )
    )

    safe_right = sum(p["muse"]["safe"] == p["should_be_safe"] for p in photos)
    caught = sum(not p["muse"]["safe"] for p in photos if not p["should_be_safe"])
    false_blocks = sum(not p["muse"]["safe"] for p in photos if p["should_be_safe"])
    print(
        f"\nsafety: {safe_right}/{len(photos)} right · caught {caught}/{sum(not p['should_be_safe'] for p in photos)} "
        f"unsafe · {false_blocks} fine photos blocked (rules were tuned on this set; not a held-out test)"
    )


if __name__ == "__main__":
    main()
