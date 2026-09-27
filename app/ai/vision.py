"""Muse looks at a photo once, offline: one plain line about what's in it, and whether it's safe to show."""

import base64
import re
from dataclasses import dataclass

from app.ai.llm import complete_json

PHOTO_SYSTEM = (
    "You look at one photo sent in a college friend group's chat. Describe what is in it in one plain, "
    "specific line (max 20 words), the way a friend would say it: the objects, food, place or activity. "
    "Never guess who anyone is, their age, race or body; say 'a person' or 'people' if they matter. "
    "Then decide if it is safe to show the group in a party game. It is NOT safe if it shows: alcohol, "
    "drugs, vaping or smoking; an ID, card, document or anything with personal numbers; a screenshot of "
    "messages or someone's private info; nudity; injury or medical stuff; or a stranger's face as the main "
    "subject. Everyday public things are fine: people in the background, street signs, landmarks, "
    "buildings, vehicles and their plates. "
    'JSON only: {"description": "...", "safe": true or false, "reason": "short reason if not safe, else empty"}'
)


# Backstop in case Muse calls a photo safe but its own description says otherwise.
# Narrower than guard.SENSITIVE: a church on a skyline is a fine photo, and round text is still guarded.
UNSAFE_WORDS = re.compile(
    r"\b(beers?|wine|alcohol\w*|liquor|shots?|drunk|vap(e|es|ing)|cigarettes?|smok\w*|weed|joint|pills?|"
    r"id card|passport|licen[cs]e card|credit card|screenshot|nude|naked|blood|injur\w*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PhotoRead:
    description: str
    safe: bool
    reason: str


def data_url(image: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(image).decode()}"


def validate(reply: dict | None) -> PhotoRead | None:
    """None if Muse gave nothing usable. A description that trips the text filter is never safe."""
    if not reply:
        return None
    description = str(reply.get("description") or "").strip()
    if not description or len(description.split()) > 30:
        return None
    safe = reply.get("safe") is True and not UNSAFE_WORDS.search(description)
    reason = "" if safe else str(reply.get("reason") or "sensitive description").strip()
    return PhotoRead(description, safe, reason)


async def read_photo(image: bytes, mime: str = "image/jpeg") -> PhotoRead | None:
    reply = await complete_json(
        PHOTO_SYSTEM, "Describe this photo.", images=[data_url(image, mime)]
    )
    return validate(reply)


POST_SYSTEM = (
    "You look at one photo someone posted to their own Instagram. Read the setting, not the person. "
    "Never say who anyone is, and never describe looks, age, race, body or clothing fit. "
    "JSON only: {"
    '"place": "where it is, as specific as the photo shows (e.g. \\"beach at sunset\\", \\"concert crowd\\"), or empty", '
    '"activity": "what is happening (e.g. \\"hiking\\", \\"cooking\\"), or empty", '
    '"people": number of people visible, '
    '"summary": "one plain line a friend would say about the scene, max 15 words, no appearance", '
    '"safe": true or false, "reason": "short reason if not safe, else empty"}. '
    "NOT safe: alcohol, drugs, vaping or smoking; IDs, cards, documents; screenshots of messages; nudity "
    "or swimwear as the focus; injury or medical; anyone who looks under 13. The poster and their "
    "friends posing is normal and fine."
)


@dataclass(frozen=True)
class PostRead:
    place: str
    activity: str
    people: int
    summary: str
    safe: bool
    reason: str

    @property
    def shows_person(self) -> bool:
        return self.people > 0

    @property
    def specific(self) -> bool:
        """False for a photo that says nothing beyond "a person" (a close-up selfie): skip it for interests."""
        from app.ai.interests import meaningful

        return meaningful(self.place) or meaningful(self.activity)


def validate_post(reply: dict | None) -> PostRead | None:
    if not reply:
        return None
    summary = str(reply.get("summary") or "").strip()
    if not summary or len(summary.split()) > 25:
        return None
    try:
        people = max(0, int(reply.get("people") or 0))
    except (TypeError, ValueError):
        people = 0
    place = str(reply.get("place") or "").strip()[:60]
    activity = str(reply.get("activity") or "").strip()[:60]
    safe = reply.get("safe") is True and not UNSAFE_WORDS.search(f"{summary} {place} {activity}")
    reason = "" if safe else str(reply.get("reason") or "sensitive description").strip()
    return PostRead(place, activity, people, summary, safe, reason)


async def read_post(
    image: bytes, caption: str | None = None, mime: str = "image/jpeg"
) -> PostRead | None:
    text = f"Caption: {caption}" if caption else "No caption."
    reply = await complete_json(POST_SYSTEM, text, images=[data_url(image, mime)])
    return validate_post(reply)
