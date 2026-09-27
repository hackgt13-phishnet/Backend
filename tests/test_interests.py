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


def test_chaos_mixes_round_types_when_material_allows():
    import random

    from app.ai.planner import chaos_candidates, chaos_choose
    from app.ai.rounds import Post

    interests = {
        "A": [Interest("f1", "mclaren", ("e1",), True)],
        "B": [Interest("horror", "a24", ("e2",), True)],
    }
    posts = [
        Post("00000000-0000-0000-0000-000000000001", "a", "post", "papaya season lando for the win")
    ]
    names = {"a": "A", "b": "B"}
    pool = candidates("little or none", [], [], interests) + chaos_candidates(
        interests, posts, names
    )
    for seed in range(20):
        chosen = chaos_choose(pool, 3, random.Random(seed))
        assert len({c.game for c in chosen}) == 3


def test_chaos_with_only_posts_still_mixes_types(monkeypatch):
    import asyncio

    from app.ai import rounds
    from app.ai.planner import plan_session
    from app.ai.rounds import Post

    async def fake(system, user):
        return None

    monkeypatch.setattr(rounds, "complete_json", fake)
    posts = [
        Post(
            "11111111-1111-4111-8111-111111111111",
            "11111111-1111-4111-8111-111111111111",
            "post",
            "papaya season lando for the win",
        )
    ]
    names = {
        "11111111-1111-4111-8111-111111111111": "A",
        "22222222-2222-4222-8222-222222222222": "B",
    }
    _branch, drafts = asyncio.run(
        plan_session([], [], {}, names, 3, chaos=True, posts=posts, variety=True)
    )
    assert len({d.game_type for d in drafts}) == 3


def test_empty_room_keeps_primary_templates_until_they_were_played(monkeypatch):
    import asyncio

    from app.ai import rounds
    from app.ai.planner import plan_session

    async def fake(system, user):
        return None

    monkeypatch.setattr(rounds, "complete_json", fake)
    names = {
        "11111111-1111-4111-8111-111111111111": "A",
        "22222222-2222-4222-8222-222222222222": "B",
    }
    _, first = asyncio.run(plan_session([], [], {}, names, 3, chaos=True, variety=True))
    by_type = {d.game_type.value: d.prompt for d in first}
    assert by_type["hot_take"] == "hot take: a weekend with this group is overrated"
    assert by_type["this_or_that"] == "a weekend with this group: overrated or underrated?"
    _, second = asyncio.run(
        plan_session(
            [],
            [],
            {},
            names,
            3,
            chaos=True,
            variety=True,
            recent_prompts={d.prompt for d in first},
        )
    )
    assert {d.prompt for d in first}.isdisjoint({d.prompt for d in second})
    assert {d.game_type for d in second} == {d.game_type for d in first}


def test_exhausted_fallback_lines_still_produce_a_game(monkeypatch):
    import asyncio

    from app.ai import rounds
    from app.ai.planner import plan_session
    from app.ai.rounds import GENERAL

    async def fake(system, user):
        return None

    monkeypatch.setattr(rounds, "complete_json", fake)
    names = {
        "11111111-1111-4111-8111-111111111111": "A",
        "22222222-2222-4222-8222-222222222222": "B",
    }
    recent = {
        "hot take: a weekend with this group is overrated",
        "hot take: a weekend with this group is underrated",
        "hot take: a weekend with this group is the move",
        "a weekend with this group: overrated or underrated?",
        *(prompt for prompt, _opts in GENERAL),
        "who's most likely to drag the group to something at 5am?",
        "who's most likely to turn a hobby into a whole personality?",
        "who's most likely to go viral for the wrong reason?",
    }
    _, drafts = asyncio.run(
        plan_session([], [], {}, names, 3, chaos=True, variety=True, recent_prompts=recent)
    )
    assert len(drafts) == 3
    assert all(d.prompt for d in drafts)


def test_recent_post_loses_to_an_unused_one():
    import random

    from app.ai.planner import chaos_candidates, chaos_choose
    from app.ai.rounds import Post

    used = Post("11111111-1111-4111-8111-111111111111", "a", "post", "one")
    fresh = Post("22222222-2222-4222-8222-222222222222", "b", "post", "two")
    pool = chaos_candidates({}, [used, fresh], {"a": "A", "b": "B"})
    for seed in range(12):
        chosen = chaos_choose(pool, 1, random.Random(seed), {used.id})
        assert chosen[0] is not None and chosen[0].post.id == fresh.id
    reused = chaos_choose(pool, 1, random.Random(0), {used.id, fresh.id})
    assert reused[0] is not None and reused[0].post is not None


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
