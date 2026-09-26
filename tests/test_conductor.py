from pathlib import Path

from app.ai.conductor import (
    ANSWER_TIMEOUT_S,
    MAX_DISCUSSION_S,
    Action,
    Conductor,
    RoomState,
)
from app.ai.conductor_features import FEATURES, Turn, features
from app.domain import RoundPhase

MEMBERS = frozenset({"maya", "dev", "sam"})
NO_MODEL = Path("/nonexistent/conductor.joblib")


def conductor() -> Conductor:
    return Conductor(NO_MODEL)  # baseline fallback keeps tests independent of private training data


def chat(*pairs: tuple[float, str]) -> tuple[Turn, ...]:
    return tuple(Turn(ts=ts, sender=who, length=20, is_question=False, has_media=False) for ts, who in pairs)


def test_features_only_use_the_past():
    turns = list(chat((0, "maya"), (5, "dev"), (100, "sam")))
    x = features(turns, now=10, is_group=True)
    assert x[FEATURES.index("secs_since_last")] == 5
    assert x[FEATURES.index("msgs_last_60s")] == 2


def test_reveals_when_everyone_answered():
    state = RoomState(RoundPhase.ANSWERING, now=10, member_ids=MEMBERS, responded_ids=MEMBERS)
    assert conductor().decide(state).action == Action.REVEAL


def test_waits_for_missing_answers_until_timeout():
    partial = frozenset({"maya"})
    waiting = RoomState(RoundPhase.ANSWERING, now=30, member_ids=MEMBERS, responded_ids=partial)
    assert conductor().decide(waiting).action == Action.WAIT
    late = RoomState(RoundPhase.ANSWERING, now=ANSWER_TIMEOUT_S + 1, member_ids=MEMBERS, responded_ids=partial)
    assert conductor().decide(late).action == Action.REVEAL


def test_stays_silent_while_people_are_talking():
    turns = chat((100, "maya"), (104, "dev"), (109, "sam"), (113, "maya"))
    state = RoomState(RoundPhase.REVEALED, now=115, member_ids=MEMBERS, revealed_at=90,
                      turns=turns, story_holder_id="dev")
    assert conductor().decide(state).action == Action.WAIT


def test_nudges_the_story_holder_once_when_it_goes_quiet():
    turns = chat((100, "maya"), (104, "sam"))
    quiet = RoomState(RoundPhase.REVEALED, now=200, member_ids=MEMBERS, revealed_at=90,
                      turns=turns, story_holder_id="dev")
    decision = conductor().decide(quiet)
    assert decision.action == Action.NUDGE and decision.target_id == "dev"

    already = RoomState(RoundPhase.REVEALED, now=200, member_ids=MEMBERS, revealed_at=90,
                        turns=turns, story_holder_id="dev", nudges_this_round=1)
    assert conductor().decide(already).action == Action.NEXT_ROUND


def test_never_nudges_someone_who_just_spoke():
    turns = chat((100, "maya"), (104, "dev"))
    state = RoomState(RoundPhase.REVEALED, now=200, member_ids=MEMBERS, revealed_at=90,
                      turns=turns, story_holder_id="dev")
    assert conductor().decide(state).action == Action.NEXT_ROUND


def test_lets_the_reveal_land_and_caps_discussion():
    early = RoomState(RoundPhase.REVEALED, now=95, member_ids=MEMBERS, revealed_at=90)
    assert conductor().decide(early).action == Action.WAIT
    turns = chat((90 + MAX_DISCUSSION_S - 1, "maya"))
    capped = RoomState(RoundPhase.REVEALED, now=90 + MAX_DISCUSSION_S, member_ids=MEMBERS,
                       revealed_at=90, turns=turns)
    assert conductor().decide(capped).action == Action.NEXT_ROUND


def test_gives_the_nudged_player_time_before_moving_on():
    turns = chat((100, "maya"), (104, "sam"))
    just_nudged = RoomState(RoundPhase.REVEALED, now=200, member_ids=MEMBERS, revealed_at=90, turns=turns,
                            story_holder_id="dev", nudges_this_round=1, last_nudge_at=190)
    assert conductor().decide(just_nudged).action == Action.WAIT
    later = RoomState(RoundPhase.REVEALED, now=230, member_ids=MEMBERS, revealed_at=90, turns=turns,
                      story_holder_id="dev", nudges_this_round=1, last_nudge_at=190)
    assert conductor().decide(later).action == Action.NEXT_ROUND
