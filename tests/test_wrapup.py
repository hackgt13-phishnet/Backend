import asyncio

from app.ai import recap
from app.ai.interests import ActivityItem
from app.services.rounds import activity_key

NAMES = ["Maya", "Dev", "Sam", "Ana", "Kofi"]
ROUNDS = [
    {
        "game_type": "who_sent_this",
        "prompt": "who sent this?",
        "reveal": "it was dev",
        "spotlight": "Dev",
        "messages_after_reveal": 6,
    }
]


def item(i: str) -> ActivityItem:
    return ActivityItem(i, "post", "public", "text")


def test_interest_cache_key_tracks_the_exact_items_not_just_the_count():
    before = activity_key([item("a"), item("b")])
    assert activity_key([item("b"), item("a")]) == before  # order doesn't matter
    # took "a" out and put "c" in: same count, different items, so the cache must be re-read
    assert activity_key([item("b"), item("c")]) != before


def test_recap_uses_muse_when_it_follows_the_rules(monkeypatch):
    async def fake(system, user):
        return {"line": "dev still owes everyone an explanation for that mario kart text"}

    monkeypatch.setattr(recap, "complete_json", fake)
    line, by = asyncio.run(recap.recap_line(ROUNDS, NAMES))
    assert by == "muse" and "dev" in line


def test_recap_falls_back_when_muse_breaks_a_rule(monkeypatch):
    replies = iter(
        [
            None,
            {"line": "maya dev sam and ana all got exposed tonight"},  # names four people
            {"line": "who got drunk at the party"},  # sensitive
        ]
    )

    async def fake(system, user):
        return next(replies)

    monkeypatch.setattr(recap, "complete_json", fake)
    for _ in range(3):
        assert asyncio.run(recap.recap_line(ROUNDS, NAMES)) == (recap.FALLBACK, "template")
