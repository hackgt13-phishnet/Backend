"""Extract conversation timing from Instagram data exports. Message text never leaves this script.

Output columns: thread (hashed), ts_ms, sender (per-thread index), length, is_question, has_media, is_group.

  uv run python scripts/extract_timing.py path/to/instagram-export [more exports...]
"""

import argparse
import csv
import glob
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEDIA_KEYS = ("photos", "videos", "gifs", "audio_files", "share")


def fix_encoding(text: str) -> str:
    """Instagram exports UTF-8 text as Latin-1 escapes."""
    try:
        return text.encode("latin1").decode("utf8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def thread_rows(folder: Path, salt: str) -> list[dict]:
    messages, participants = [], []
    for part in sorted(folder.glob("message_*.json")):
        data = json.loads(part.read_text())
        participants = [p["name"] for p in data.get("participants", [])]
        messages.extend(data.get("messages", []))
    if len(messages) < 2:
        return []

    thread = hashlib.sha256(f"{salt}:{folder.name}".encode()).hexdigest()[:16]
    senders: dict[str, int] = {}
    rows = []
    for m in sorted(messages, key=lambda m: m["timestamp_ms"]):
        text = fix_encoding(m.get("content", ""))
        if text.endswith(("reacted to your message", "liked a message")):
            continue  # reactions aren't conversation turns
        rows.append(
            {
                "thread": thread,
                "ts_ms": m["timestamp_ms"],
                "sender": senders.setdefault(m.get("sender_name", "?"), len(senders)),
                "length": len(text),
                "is_question": int("?" in text),
                "has_media": int(any(k in m for k in MEDIA_KEYS)),
                "is_group": int(len(participants) > 2),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("exports", nargs="+", type=Path, help="Instagram export folders")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "private" / "timing.csv")
    args = parser.parse_args()

    rows = []
    for export in args.exports:
        salt = hashlib.sha256(str(export.resolve()).encode()).hexdigest()
        pattern = str(export / "**" / "messages" / "inbox" / "*")
        for folder in sorted(glob.glob(pattern, recursive=True)):
            rows.extend(thread_rows(Path(folder), salt))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    threads = len({r["thread"] for r in rows})
    print(f"wrote {len(rows)} messages from {threads} threads to {args.out}")


if __name__ == "__main__":
    main()
