"""Understand each player from their own activity, then find where players overlap or clash.

Both steps are Muse's job: reading loose, slangy posts into interests, and matching interests across
people ("papaya season" and "forza ferrari" are both F1, on rival teams). Everything Muse returns must
cite evidence we gave it, or it's dropped.
"""

import json
import re
from dataclasses import dataclass, field

from app.ai.guard import is_sensitive
from app.ai.llm import complete_json

MAX_INTERESTS = 6
MAX_LINKS = 8

EXTRACT_SYSTEM = (
    "You read one person's recent Instagram activity and list what they're genuinely into. "
    'Reply with JSON: {"interests": [{"topic": "...", "detail": "...", "evidence": ["<item id>", ...]}]}. '
    f"At most {MAX_INTERESTS} interests. topic: 1-4 lowercase words. detail: under 12 words, specific "
    "(a team, a show, a stance), in the person's own vibe, using ONLY items marked public (likes, saves "
    "and follows are private: they can support an interest but never appear in its detail). "
    "Every interest cites at least one item id. "
    "Never infer health, relationships, religion, politics, money, sexuality or heritage."
)

LINK_SYSTEM = (
    "You get several friends' interests. Find where two or more of them overlap (same thing, even if "
    "worded differently) or clash (same topic, opposite sides). Reply with JSON: "
    '{"links": [{"kind": "overlap" or "clash", "topic": "...", "angle": "...", '
    '"players": {"<name>": <interest index>, ...}}]}. angle: under 15 words, what makes it fun to '
    f"talk about. At most {MAX_LINKS} links. Only use the names and indexes given."
)


@dataclass(frozen=True)
class ActivityItem:
    id: str
    kind: str  # post, story, liked_reel, saved, follow
    visibility: str  # public or private
    text: str


# Things any photo of a person "shows" that say nothing about what they're into.
GENERIC_SCENE = {
    "", "posing", "smiling", "selfie", "taking a selfie", "looking at the camera", "standing",
    "sitting", "portrait", "close-up", "closeup", "indoors", "outdoors",
}


def meaningful(value: str | None) -> bool:
    return (value or "").strip().lower() not in GENERIC_SCENE


def post_text(caption: str | None, read: dict | None, location: str | None) -> str:
    """What Muse reads for a post: its caption plus what the photo shows and where. Empty means the
    post says nothing specific (no caption, and a photo with no place or activity) and is skipped."""
    parts = [caption.strip()] if caption and caption.strip() else []
    if read and (meaningful(read.get("place")) or meaningful(read.get("activity"))):
        parts.append(f"[photo: {read.get('summary') or read.get('activity') or read.get('place')}]")
    if location:
        parts.append(f"[at {location}]")
    return " ".join(parts)


@dataclass(frozen=True)
class Interest:
    topic: str
    detail: str
    evidence: tuple[str, ...]
    public: bool  # False = learned only from likes/saves/follows: usable as a topic, never quoted

    def shareable(self) -> str:
        """What the round writer may see: private-only interests are reduced to the bare topic."""
        return f"{self.topic}: {self.detail}" if self.public else self.topic


@dataclass(frozen=True)
class Link:
    kind: str  # overlap or clash between players, or solo (one player's own interest)
    topic: str
    angle: str
    players: dict[str, int] = field(
        default_factory=dict
    )  # name -> index into that player's interests


WORD = re.compile(r"[a-z0-9]{4,}")


def public_detail(topic: str, detail: str, evidence: list["ActivityItem"]) -> str:
    """The detail a round may quote. If it uses a word that only a private item (a like, save or follow)
    contains, it would reveal that item, so fall back to the person's own words from a public post."""
    public = [i.text for i in evidence if i.visibility == "public"]
    private = " ".join(i.text for i in evidence if i.visibility != "public").lower()
    allowed = set(WORD.findall(" ".join([topic, *public]).lower()))
    leaks = {w for w in WORD.findall(detail.lower()) if w in private and w not in allowed}
    if not leaks:
        return detail
    return " ".join(public[0].split()[:12]) if public else ""


def validate_interests(reply: dict | None, items: list[ActivityItem]) -> list[Interest]:
    by_id = {i.id: i for i in items}
    out = []
    for raw in (reply or {}).get("interests", [])[:MAX_INTERESTS]:
        if not isinstance(raw, dict):
            continue
        topic, detail = (
            str(raw.get("topic", "")).strip().lower(),
            str(raw.get("detail", "")).strip(),
        )
        evidence = tuple(e for e in raw.get("evidence", []) if e in by_id)
        if not topic or len(topic.split()) > 4 or not evidence or is_sensitive(f"{topic} {detail}"):
            continue
        public = any(by_id[e].visibility == "public" for e in evidence)
        out.append(
            Interest(
                topic,
                public_detail(topic, detail[:120], [by_id[e] for e in evidence]),
                evidence,
                public,
            )
        )
    return out


def validate_links(reply: dict | None, interests: dict[str, list[Interest]]) -> list[Link]:
    out = []
    for raw in (reply or {}).get("links", [])[:MAX_LINKS]:
        if not isinstance(raw, dict) or raw.get("kind") not in ("overlap", "clash"):
            continue
        players = raw.get("players", {})
        if not isinstance(players, dict) or len(players) < 2:
            continue
        ok = all(
            name in interests and isinstance(idx, int) and 0 <= idx < len(interests[name])
            for name, idx in players.items()
        )
        topic, angle = str(raw.get("topic", "")).strip().lower(), str(raw.get("angle", "")).strip()
        if ok and topic and not is_sensitive(f"{topic} {angle}"):
            out.append(Link(raw["kind"], topic, angle[:140], dict(players)))
    return out


async def extract_interests(items: list[ActivityItem]) -> list[Interest]:
    if not items:
        return []
    reply = await complete_json(
        EXTRACT_SYSTEM,
        json.dumps(
            [
                {"id": i.id, "kind": i.kind, "visibility": i.visibility, "text": i.text}
                for i in items
            ],
            ensure_ascii=False,
        ),
    )
    return validate_interests(reply, items)


async def find_links(interests: dict[str, list[Interest]]) -> list[Link]:
    if sum(1 for v in interests.values() if v) < 2:
        return []
    payload = {
        name: [{"index": n, "interest": i.shareable()} for n, i in enumerate(v)]
        for name, v in interests.items()
    }
    reply = await complete_json(LINK_SYSTEM, json.dumps(payload, ensure_ascii=False))
    return validate_links(reply, interests)
