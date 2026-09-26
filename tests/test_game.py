import pytest

from app.domain import RoundPhase
from app.services.game import assert_transition, can_transition


def test_pending_round_can_open_answers():
    assert can_transition(RoundPhase.PENDING, RoundPhase.ANSWERING)


def test_answering_round_cannot_complete_without_reveal():
    with pytest.raises(ValueError):
        assert_transition(RoundPhase.ANSWERING, RoundPhase.COMPLETE)
