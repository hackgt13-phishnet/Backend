"""Short human names for moments ("Lisbon spite trip", "the rice cooker")."""

import json

from app.ai.llm import complete_json

SYSTEM = (
    "You name moments from a friend group's chat history. "
    'Reply with JSON: {"name": "..."}. The name is 2-5 lowercase words, the way the friends '
    'would refer to it themselves (e.g. "the 3am fire alarm", "maya\'s lisbon era"). '
    "No emojis, no quotes, never mention health, relationships, religion, politics or money."
)
MAX_NAME_CHARS = 40
MAX_SAMPLES = 12


def valid_name(name: object) -> bool:
    return isinstance(name, str) and 2 <= len(name.split()) <= 5 and len(name) <= MAX_NAME_CHARS


def fallback_name(keywords: list[str]) -> str:
    return " · ".join(keywords[:2]) if keywords else "untitled moment"


async def name_moment(texts: list[str], kind: str, keywords: list[str]) -> str:
    """Muse names the moment. Falls back to keywords if no model is configured or the reply is invalid."""
    user = json.dumps({"kind": kind, "messages": texts[:MAX_SAMPLES]}, ensure_ascii=False)
    reply = await complete_json(SYSTEM, user)
    name = (reply or {}).get("name")
    if valid_name(name):
        return name.strip().lower()
    return fallback_name(keywords)
