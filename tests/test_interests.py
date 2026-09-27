import asyncio

from app.ai import rounds
from app.ai.interests import ActivityItem, Interest, Link, validate_interests, validate_links
from app.ai.planner import branch_for, candidates, choose, plan_session
from app.domain import GameType

ITEMS = [
    ActivityItem("p1", "post", "public", "papaya season 🧡 lando for the win"),
    ActivityItem("l1", "liked_reel", "private", "mclaren pit stop in 1.8 seconds"),
    ActivityItem("s1", "saved", "private", "best horror movies of the decade"),
]


def test_interests_must_cite_real_evidence_and_skip_sensitive_topics():
    reply = {
        "interests": [
            {"topic": "formula 1", "detail": "mclaren or nothing", "evidence": ["p1", "l1"]},
            {"topic": "horror movies", "detail": "saves horror lists", "evidence": ["s1"]},
            {"topic": "made up", "detail": "no evidence", "evidence": ["nope"]},
            {"topic": "therapy", "detail": "mentions therapy", "evidence": ["p1"]},
        ]
    }
    found = validate_interests(reply, ITEMS)
    assert [i.topic for i in found] == ["formula 1", "horror movies"]
    assert found[0].public and not found[1].public


def test_private_only_interests_are_never_quoted():
    private = Interest("horror movies", "saves every horror list", ("s1",), public=False)
    public = Interest("formula 1", "mclaren or nothing", ("p1",), public=True)
    assert private.shareable() == "horror movies"
    assert public.shareable() == "formula 1: mclaren or nothing"


def test_links_must_point_at_real_interests_of_real_players():
    interests = {
        "Dev": [Interest("f1", "ferrari", ("a",), True)],
        "Riya": [Interest("f1", "mclaren", ("b",), True)],
    }
    reply = {
        "links": [
            {
                "kind": "clash",
                "topic": "f1",
                "angle": "ferrari vs mclaren",
                "players": {"Dev": 0, "Riya": 0},
            },
            {"kind": "overlap", "topic": "x", "angle": "", "players": {"Dev": 0, "Ghost": 0}},
            {"kind": "overlap", "topic": "y", "angle": "", "players": {"Dev": 5, "Riya": 0}},
            {"kind": "friends", "topic": "z", "angle": "", "players": {"Dev": 0, "Riya": 0}},
        ]
    }
    links = validate_links(reply, interests)
    assert len(links) == 1 and links[0].kind == "clash"


def test_branch_follows_how_much_shared_history_there_is():
    assert branch_for(5, 0) == "a lot"
    assert branch_for(1, 3) == "some"
    assert branch_for(0, 2) == "little or none"
    assert branch_for(0, 0) == "nothing usable"


def test_no_interest_is_used_twice_and_spotlight_rotates():
    interests = {
        "Dev": [Interest("f1", "ferrari", ("a",), True), Interest("kendrick", "gnx", ("b",), True)],
        "Riya": [Interest("f1", "mclaren", ("c",), True), Interest("anime", "jjk", ("d",), True)],
    }
    links = [
        Link("clash", "f1", "rivals", {"Dev": 0, "Riya": 0}),
        Link("clash", "drivers", "leclerc vs norris", {"Dev": 0, "Riya": 0}),
    ]
    picked = choose(candidates("little or none", [], links, interests), 3)
    first, second, third = picked
    assert first.link.topic == "f1" and first.game == GameType.HOT_TAKE
    assert {second.link.players.popitem()[0], third.link.players.popitem()[0]} == {"Dev", "Riya"}


def test_nothing_usable_falls_back_to_general_rounds(monkeypatch):
    async def fake(system, user):
        return None

    monkeypatch.setattr(rounds, "complete_json", fake)
    branch, drafts = asyncio.run(plan_session([], [], {}, {"a": "A", "b": "B"}, 3))
    assert branch == "nothing usable"
    assert all(d.source == "general" and d.game_type == GameType.THIS_OR_THAT for d in drafts)


def test_private_likes_never_leak_into_a_quotable_detail():
    from app.ai.interests import ActivityItem, public_detail

    dev = [
        ActivityItem("1", "follow", "private", "scuderia ferrari"),
        ActivityItem("2", "liked_reel", "private", "leclerc onboard monaco pole lap"),
        ActivityItem(
            "3", "post", "public", "ferrari strategy department needs to be investigated fr"
        ),
    ]
    # "leclerc" and "monaco" only appear in a reel Dev privately liked: the detail would reveal it.
    safe = public_detail("formula 1", "forza ferrari, leclerc monaco lap stan", dev)
    assert "leclerc" not in safe and "monaco" not in safe
    assert safe == "ferrari strategy department needs to be investigated fr"
    # Details built from public posts pass through untouched.
    assert public_detail("formula 1", "ferrari strategy slander", dev) == "ferrari strategy slander"
    # Private-only interests have nothing public to quote.
    assert public_detail("formula 1", "leclerc monaco stan", dev[:2]) == ""
