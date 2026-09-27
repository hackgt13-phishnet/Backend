"""The conductor: decides what the game master does next. Default is to say nothing."""

import math
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import joblib
import numpy as np

from app.ai.conductor_features import FEATURES, Turn, features
from app.domain import RoundPhase

MODELS = Path(__file__).resolve().parents[2] / "models"
MAX_NUDGES_PER_ROUND = 1


@dataclass(frozen=True)
class Pace:
    """How fast the game moves. The AI still decides when to act; the demo pace predicts a shorter silence."""

    name: str
    model_path: Path
    silence_window_s: float  # what "going quiet" means for this pace's model
    speak_threshold: float  # act only when the model is fairly sure the chat is going quiet
    min_discussion_s: float  # let a reveal land before considering anything
    max_discussion_s: float  # retained for simulation compatibility; not an advance gate
    answer_timeout_s: float  # retained for simulation compatibility; never skips answers
    nudge_grace_s: float  # after nudging someone, give them time to answer before moving on
    tick_s: float  # how often the game master checks each room
    min_silence_s: float  # never speak within this long of someone's message


PACES = {
    "normal": Pace("normal", MODELS / "conductor.joblib", 60, 0.7, 20, 360, 90, 30, 5, 15),
    # demo threshold from held-out data: right 76% of the time it acts, catches 85% of quiet moments
    "demo": Pace("demo", MODELS / "conductor_demo.joblib", 20, 0.5, 8, 100, 25, 10, 2, 8),
}
NORMAL = PACES["normal"]
# Kept for readability in tests and docs; the live values come from the active Pace.
SPEAK_THRESHOLD, MIN_DISCUSSION_S, MAX_DISCUSSION_S = (
    NORMAL.speak_threshold,
    NORMAL.min_discussion_s,
    NORMAL.max_discussion_s,
)
ANSWER_TIMEOUT_S, NUDGE_GRACE_S = NORMAL.answer_timeout_s, NORMAL.nudge_grace_s


def pace_from_env() -> Pace:
    return PACES[os.environ.get("GAME_PACE", "normal")]


class Action(StrEnum):
    WAIT = "wait"
    REVEAL = "reveal"
    NUDGE = "nudge"
    NEXT_ROUND = "next_round"


@dataclass(frozen=True)
class RoomState:
    phase: RoundPhase
    now: float
    member_ids: frozenset[str]
    responded_ids: frozenset[str] = frozenset()
    round_opened_at: float = 0.0
    revealed_at: float | None = None
    turns: tuple[Turn, ...] = ()
    nudges_this_round: int = 0
    last_nudge_at: float | None = None
    # The person with a story behind this round (e.g. whoever sent the item). Only they get nudged.
    story_holder_id: str | None = None


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    p_silence: float | None = None
    target_id: str | None = None
    model_source: str = "model"
    features: dict[str, float] = field(default_factory=dict)


class Conductor:
    def __init__(self, path: Path | None = None, pace: Pace = NORMAL):
        self.pace = pace
        path = path or pace.model_path
        self.model = None
        if path.exists():
            bundle = joblib.load(path)
            if bundle["features"] != FEATURES:
                raise ValueError("conductor model was trained on different features; retrain it")
            self.model = bundle["model"]

    @property
    def source(self) -> str:
        return "model" if self.model is not None else "fallback"

    def p_silence(self, turns: list[Turn], now: float, is_group: bool) -> tuple[float, list[float]]:
        x = features(turns, now, is_group)
        if self.model is not None:
            return float(self.model.predict_proba(np.array([x]))[0, 1]), x
        # No trained model on this machine: time since the last message only (the baseline).
        mid = 0.75 * self.pace.silence_window_s
        return 1 / (1 + math.exp(-(x[0] - mid) / (mid / 4.5))), x

    def decide(self, state: RoomState) -> Decision:
        if state.phase == RoundPhase.ANSWERING:
            if state.member_ids <= state.responded_ids:
                return Decision(Action.REVEAL, "everyone answered")
            waiting = len(state.member_ids - state.responded_ids)
            return Decision(
                Action.WAIT, f"waiting on {waiting} answer{'s' if waiting != 1 else ''}"
            )

        if state.phase != RoundPhase.REVEALED or state.revealed_at is None:
            return Decision(Action.WAIT, "no round in discussion")

        since_reveal = state.now - state.revealed_at
        if since_reveal < self.pace.min_discussion_s:
            return Decision(Action.WAIT, "letting the reveal land")

        # With no chat history, measure silence from the reveal, not the Unix epoch.
        p, x = self.p_silence(
            list(state.turns),
            state.now if state.turns else since_reveal,
            is_group=len(state.member_ids) > 2,
        )
        detail = dict(zip(FEATURES, x, strict=True))
        if state.turns and state.now - state.turns[-1].ts < self.pace.min_silence_s:
            return Decision(Action.WAIT, "someone just spoke", p, None, self.source, detail)
        if p < self.pace.speak_threshold:
            return Decision(
                Action.WAIT, "conversation is still going", p, None, self.source, detail
            )
        if (
            state.last_nudge_at is not None
            and state.now - state.last_nudge_at < self.pace.nudge_grace_s
        ):
            return Decision(
                Action.WAIT, "giving the nudged player time to answer", p, None, self.source, detail
            )

        last_speaker = state.turns[-1].sender if state.turns else None
        can_nudge = (
            state.nudges_this_round < MAX_NUDGES_PER_ROUND
            and state.story_holder_id is not None
            and state.story_holder_id != last_speaker
        )
        if can_nudge:
            return Decision(
                Action.NUDGE,
                "going quiet and someone has an untold story",
                p,
                state.story_holder_id,
                self.source,
                detail,
            )
        return Decision(
            Action.NEXT_ROUND, "conversation has wound down", p, None, self.source, detail
        )
