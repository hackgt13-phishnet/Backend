import uuid

import pytest
from fastapi import HTTPException

from app.ai.judge import Answer, Verdict, fallback
from app.domain import GameType, PublicRound, RoundDraft, RoundPhase
from app.services.game import (
    ai_round,
    assert_transition,
    can_transition,
    check_submission,
    reveal_result,
)

A, B = str(uuid.uuid4()), str(uuid.uuid4())
NAMES = {A: "Kofi", B: "Maya"}


def draft(game_type, options, answer=None):
    return RoundDraft(
        game_type=game_type,
        prompt="p",
        options=options,
        answer=answer,
        source_item_ids=[uuid.uuid4()],
        reveal_copy="r",
    )


def test_who_sent_this_options_are_players_and_answer_is_secret_profile():
    stored = ai_round(draft(GameType.WHO_SENT_THIS, ["Kofi", "Maya"], "Maya"), NAMES)
    assert stored["options"][1] == {"id": B, "label": "Maya", "profile_id": B}
    assert stored["answer"] == {"correct_profile_id": B}


def test_opinion_rounds_get_letter_ids_and_are_judged():
    stored = ai_round(draft(GameType.THIS_OR_THAT, ["rice cooker", "stovetop"]), NAMES)
    assert [o["id"] for o in stored["options"]] == ["a", "b"]
    assert stored["answer"] == {"judge": True}


def test_opinion_round_requires_a_why():
    row = {
        "phase": "answering",
        "submitted_profile_ids": [],
        "options": [{"id": "a", "label": "x"}],
    }
    secret = {"eligible_profile_ids": [uuid.UUID(A)], "answer": {"judge": True}}
    with pytest.raises(HTTPException) as error:
        check_submission(row, secret, uuid.UUID(A), "a", "  ")
    assert error.value.status_code == 422
    check_submission(row, secret, uuid.UUID(A), "a", "because")


def test_judged_reveal_gives_the_point_to_the_winner_only():
    secret = {"eligible_profile_ids": [uuid.UUID(A), uuid.UUID(B)], "reveal_copy": "r"}
    responses = [
        {"profile_id": uuid.UUID(A), "value": {"choice": "a", "why": "short"}},
        {"profile_id": uuid.UUID(B), "value": {"choice": "a", "why": "the funny one"}},
    ]
    reveal = reveal_result(secret, responses, Verdict(B, "maya ate", "muse"))
    assert [(r["points"], r["why"]) for r in reveal["results"]] == [
        (0, "short"),
        (1, "the funny one"),
    ]
    assert reveal["winner_profile_id"] == B


def test_guess_reveal_scores_only_the_right_answer():
    secret = {
        "eligible_profile_ids": [uuid.UUID(A), uuid.UUID(B)],
        "reveal_copy": "r",
        "answer": {"correct_profile_id": A},
    }
    responses = [{"profile_id": uuid.UUID(A), "value": B}, {"profile_id": uuid.UUID(B), "value": B}]
    assert [r["points"] for r in reveal_result(secret, responses)["results"]] == [0, 0]


def test_judge_fallback_is_deterministic():
    answers = [Answer(A, "Kofi", "x", "ok"), Answer(B, "Maya", "y", "a much more specific answer")]
    assert fallback(answers).winner_profile_id == B


def test_fixture_options_without_ids_still_validate():
    row = {
        "id": uuid.uuid4(),
        "room_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "ordinal": 1,
        "game_type": "who_sent_this",
        "phase": "answering",
        "prompt": "p",
        "media": {},
        "options": [{"profile_id": A, "label": "Kofi"}],
        "required_response_count": 2,
        "submitted_profile_ids": [],
        "reveal": None,
        "revision": 1,
    }
    assert PublicRound.model_validate(row).options[0].id == A


def test_pending_round_can_open_answers():
    assert can_transition(RoundPhase.PENDING, RoundPhase.ANSWERING)


def test_answering_round_cannot_complete_without_reveal():
    with pytest.raises(ValueError):
        assert_transition(RoundPhase.ANSWERING, RoundPhase.COMPLETE)


def test_async_round_reveals_on_the_players_still_here():
    from uuid import uuid4

    from app.services.game import reveal_result

    a, b, gone = uuid4(), uuid4(), uuid4()
    secret = {
        "eligible_profile_ids": [a, b, gone],
        "answer": {"correct_profile_id": str(a)},
        "reveal_copy": "it was a",
        "source_note": None,
    }
    answers = [{"profile_id": a, "value": str(a)}, {"profile_id": b, "value": str(a)}]
    # Everyone dealt in: still waiting on the player who left.
    with pytest.raises(HTTPException):
        reveal_result(secret, answers)
    # Only the players still here: it reveals, and scores those two.
    revealed = reveal_result(secret, answers, required={a, b})
    assert {r["profile_id"] for r in revealed["results"]} == {str(a), str(b)}


def test_opinion_round_everyone_left_reveals_empty():
    from app.services.game import reveal_result

    secret = {
        "eligible_profile_ids": [],
        "answer": {"judge": True},
        "reveal_copy": "takes are in",
        "source_note": None,
    }
    assert reveal_result(secret, [], required=set())["results"] == []
