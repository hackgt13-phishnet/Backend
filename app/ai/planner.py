"""Plan a session: pick the flowchart branch from how much shared history the room has, then let the
material decide each round's type. The planning is plain code; the AI work is upstream (moments,
interests, links) and downstream (Muse writing each round).
"""

import asyncio
import random
from dataclasses import dataclass
from uuid import UUID

from app.ai.interests import Interest, Link
from app.ai.picker import Pick
from app.ai.rounds import (
    Post,
    general_round,
    hot_take,
    most_likely_from_interests,
    most_likely_to,
    this_or_that,
    who_posted_this,
    who_sent_this,
)
from app.domain import GameType, RoundDraft

A_LOT = 3  # playable moments at which shared history carries the game
VARIETY_BONUS = 0.35  # prefer a different round type from the previous round
LINKS_WHEN_HISTORY_IS_RICH = (
    0.8  # interest rounds still count, just a little less, when history is rich
)
MAX_SPLIT = 0.25  # mean p(1-p) when the room is perfectly divided
SPOTLIGHT_PENALTY = 0.2  # per earlier round built around the same player's interests


@dataclass(frozen=True)
class Candidate:
    game: GameType
    score: float
    key: str  # the material; each piece is used once per session
    pick: Pick | None = None
    link: Link | None = None
    post: Post | None = None
    refs: frozenset[tuple[str, int]] = (
        frozenset()
    )  # (player, interest index): no interest is reused


def branch_for(playable_moments: int, interest_material: int) -> str:
    if playable_moments >= A_LOT:
        return "a lot"
    if playable_moments > 0:
        return "some"
    return "little or none" if interest_material else "nothing usable"


def candidates(
    branch: str,
    picks: list[Pick],
    links: list[Link],
    interests: dict[str, list[Interest]] | None = None,
) -> list[Candidate]:
    """Scores are comparable across round types (0-1). Guessing rounds are the signature game."""
    out = []
    if branch in ("a lot", "some"):
        for p in picks:
            key = f"m:{p.moment.id}"
            # Guessing needs a split room. Voting needs EVERYONE to know it, so it's scored by the least-informed player.
            out.append(
                Candidate(
                    GameType.WHO_SENT_THIS, 0.5 + 0.5 * min(1.0, p.split / MAX_SPLIT), key, pick=p
                )
            )
            out.append(Candidate(GameType.MOST_LIKELY_TO, min(p.p_known.values()), key, pick=p))
    if branch != "nothing usable":
        weight = LINKS_WHEN_HISTORY_IS_RICH if branch == "a lot" else 1.0
        for n, link in enumerate(links):
            refs = frozenset(link.players.items())
            if link.kind == "clash":
                out.append(
                    Candidate(GameType.HOT_TAKE, 0.8 * weight, f"l:{n}", link=link, refs=refs)
                )
            else:
                out.append(
                    Candidate(GameType.THIS_OR_THAT, 0.7 * weight, f"l:{n}", link=link, refs=refs)
                )
                out.append(
                    Candidate(GameType.HOT_TAKE, 0.55 * weight, f"l:{n}", link=link, refs=refs)
                )
        # One player's own public interest: the room learns something new about them, and they get the turn.
        for name, found in (interests or {}).items():
            for idx, interest in enumerate(found):
                if interest.public:
                    solo = Link("solo", interest.topic, interest.detail, {name: idx})
                    out.append(
                        Candidate(
                            GameType.THIS_OR_THAT,
                            0.45 * weight,
                            f"s:{name}:{idx}",
                            link=solo,
                            refs=frozenset({(name, idx)}),
                        )
                    )
    return out


def choose(pool: list[Candidate], rounds: int) -> list[Candidate | None]:
    """Greedy: best-scoring unused material each round, nudged toward variety. None = general round."""
    chosen: list[Candidate | None] = []
    used: set[str] = set()
    used_refs: set[tuple[str, int]] = set()
    spotlight: dict[str, int] = {}
    previous: GameType | None = None

    def value(c: Candidate) -> float:
        variety = VARIETY_BONUS if c.game != previous else 0
        repeat = SPOTLIGHT_PENALTY * sum(spotlight.get(name, 0) for name, _ in c.refs)
        return c.score + variety - repeat

    for _ in range(rounds):
        options = [c for c in pool if c.key not in used and not (c.refs & used_refs)]
        if not options:
            chosen.append(None)
            continue
        best = max(options, key=value)
        chosen.append(best)
        used.add(best.key)
        used_refs |= best.refs
        for name, _ in best.refs:
            spotlight[name] = spotlight.get(name, 0) + 1
        previous = best.game
    return chosen


def chaos_candidates(
    interests: dict[str, list[Interest]], posts: list[Post], names: dict[str, str]
) -> list[Candidate]:
    """Material every round type can use in a small room, where shared chat moments are off limits
    (only threads whose whole membership is playing may be quoted)."""
    out = [
        Candidate(GameType.WHO_SENT_THIS, 0.5, f"p:{p.id}", post=p)
        for p in posts
        if p.owner_id in names
    ]
    if len(names) >= 2 and any(i.public for found in interests.values() for i in found):
        out.append(Candidate(GameType.MOST_LIKELY_TO, 0.5, "mlt:interests"))
    for name, found in interests.items():
        for idx, interest in enumerate(found):
            if interest.public:
                solo = Link("solo", interest.topic, interest.detail, {name: idx})
                out.append(
                    Candidate(
                        GameType.HOT_TAKE,
                        0.4,
                        f"h:{name}:{idx}",
                        link=solo,
                        refs=frozenset({(name, idx)}),
                    )
                )
    return out


def chaos_choose(pool: list[Candidate], rounds: int, rng: random.Random) -> list[Candidate | None]:
    """Chaos: a random round type each round, all different when the material allows, then the
    best-scoring unused material of that type (a random pick among the top few, for variety)."""
    by_game: dict[GameType, list[Candidate]] = {}
    for c in pool:
        by_game.setdefault(c.game, []).append(c)
    games = list(by_game)
    rng.shuffle(games)
    order = games[:rounds]
    while len(order) < rounds and games:
        order.append(rng.choice([g for g in games if g != order[-1]] or games))

    chosen: list[Candidate | None] = []
    used: set[str] = set()
    used_refs: set[tuple[str, int]] = set()
    for game in order + [None] * (rounds - len(order)):
        options = [
            c for c in by_game.get(game, []) if c.key not in used and not (c.refs & used_refs)
        ]
        if not options:
            options = [c for c in pool if c.key not in used and not (c.refs & used_refs)]
        if not options:
            chosen.append(None)
            continue
        options.sort(key=lambda c: c.score, reverse=True)
        best = rng.choice(options[:3])
        chosen.append(best)
        used.add(best.key)
        used_refs |= best.refs
    return chosen


def evidence_ids(link: Link, interests: dict[str, list[Interest]]) -> list[str]:
    ids = [e for name, idx in link.players.items() for e in interests[name][idx].evidence]
    return list(dict.fromkeys(ids))[:8]


async def write(
    c: Candidate | None, ordinal: int, names: dict[str, str], interests: dict[str, list[Interest]]
) -> RoundDraft:
    if c is None:
        return general_round(ordinal)
    name_to_id = {v: k for k, v in names.items()}
    rng = random.Random(f"{c.key}:{ordinal}")
    if c.post is not None:
        return await who_posted_this(c.post, names)
    if c.game == GameType.MOST_LIKELY_TO and c.pick is None:
        return await most_likely_from_interests(interests, names, rng)
    if c.game == GameType.WHO_SENT_THIS:
        return await who_sent_this(c.pick, names, rng)
    if c.game == GameType.MOST_LIKELY_TO:
        return await most_likely_to(c.pick, names, rng)
    writer = hot_take if c.game == GameType.HOT_TAKE else this_or_that
    draft = await writer(c.link, names, name_to_id, interests)
    return draft.model_copy(
        update={"source_item_ids": [UUID(e) for e in evidence_ids(c.link, interests)]}
    )


async def plan_session(
    picks: list[Pick],
    links: list[Link],
    interests: dict[str, list[Interest]],
    names: dict[str, str],
    rounds: int,
    chaos: bool = False,
    posts: list[Post] | None = None,
) -> tuple[str, list[RoundDraft]]:
    public_interests = sum(1 for found in interests.values() for i in found if i.public)
    branch = branch_for(len(picks), len(links) + public_interests)
    pool = candidates(branch, picks, links, interests)
    if chaos:
        pool += chaos_candidates(interests, posts or [], names)
        chosen = chaos_choose(pool, rounds, random.Random())
    else:
        chosen = choose(pool, rounds)
    drafts = await asyncio.gather(
        *(write(c, n + 1, names, interests) for n, c in enumerate(chosen))
    )
    return branch, list(drafts)
