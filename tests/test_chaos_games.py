import asyncio
import random
import uuid

import pytest
from fastapi import HTTPException

from app.ai import rounds
from app.ai.interests import Interest, Link
from app.ai.judge import Verdict
from app.ai.picker import ItemView
from app.ai.planner import CHAOS_GAMES, chaos_candidates, chaos_choose
from app.domain import GameType
from app.services.game import OPEN, ai_round, check_submission, judged_result

NAMES = {str(uuid.uuid5(uuid.NAMESPACE_DNS, n)): n for n in ["Dev", "Riya"]}
DEV, RIYA = list(NAMES)
F1 = {"Dev": [Interest("formula 1", "forza ferrari, up for 5am race starts", ("1",), True)]}


def test_hot_take_is_open_and_rejects_closed_questions(monkeypatch):
    replies = iter(
        [
            {"prompt": "is ferrari better than mclaren?", "reveal": "x"},  # yes/no: rejected
            {
                "prompt": "what's the most unhinged thing about ferrari strategy calls?",
                "reveal": "the takes are in",
            },
        ]
    )

    async def fake(system, user):
        return next(replies)

    monkeypatch.setattr(rounds, "complete_json", fake)
    draft = asyncio.run(
        rounds.hot_take(
            Link("solo", "formula 1", "", {"Dev": 0}), NAMES, {v: k for k, v in NAMES.items()}, F1
        )
    )
    assert draft.options == [] and draft.prompt.startswith("what's the most unhinged")
    assert rounds.CLOSED_QUESTION.search("pizza or tacos?")
    assert not rounds.CLOSED_QUESTION.search("what's your worst ferrari take?")


def test_open_round_takes_a_typed_answer_and_needs_one():
    row = {"phase": "answering", "submitted_profile_ids": [], "options": []}
    secret = {"eligible_profile_ids": [uuid.UUID(DEV)], "answer": {"judge": True}}
    check_submission(row, secret, uuid.UUID(DEV), OPEN, "ferrari strategy is performance art")
    with pytest.raises(HTTPException):
        check_submission(row, secret, uuid.UUID(DEV), OPEN, "  ")
    with pytest.raises(HTTPException):
        check_submission(row, secret, uuid.UUID(DEV), "a", "an option id isn't valid here")
    result = judged_result(
        {"reveal_copy": "takes are in"},
        [{"profile_id": DEV, "value": {"choice": OPEN, "why": "it's performance art"}}],
        Verdict(DEV, "dev understood the assignment", "muse"),
    )
    assert (
        result["results"][0]["choice"] is None
        and result["results"][0]["why"] == "it's performance art"
    )


def test_chaos_is_only_who_sent_this_and_hot_take_and_uses_reels():
    reel = ItemView(
        str(uuid.uuid4()),
        DEV,
        frozenset(NAMES),
        "i just watched a squirrel eat a whole bagel",
        "reel",
    )
    pool = chaos_candidates(F1, [], NAMES, [reel])
    assert {c.game for c in pool} <= CHAOS_GAMES and any(c.reel is reel for c in pool)
    picked = chaos_choose(pool, 3, random.Random(1))
    assert {c.game for c in picked if c} == CHAOS_GAMES


def test_a_reel_round_shows_as_a_reel_card(monkeypatch):
    async def fake(system, user):
        return {"reveal": "dev and his squirrel content"}

    monkeypatch.setattr(rounds, "complete_json", fake)
    reel = ItemView(
        str(uuid.uuid4()),
        DEV,
        frozenset(NAMES),
        "i just watched a squirrel eat a whole bagel",
        "reel",
    )
    draft = asyncio.run(rounds.who_sent_this_reel(reel, NAMES))
    stored = ai_round(draft, NAMES)
    assert draft.prompt == "who sent this reel?" and draft.answer == "Dev"
    assert stored["media"] == {
        "type": "reel",
        "caption": "i just watched a squirrel eat a whole bagel",
    }
    assert stored["answer"] == {"correct_profile_id": DEV}
    assert GameType.WHO_SENT_THIS.value == stored["game_type"]
