"""The conductor: decides what the game master does next. Default is to say nothing."""

import math
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import joblib
import numpy as np

from app.ai.conductor_features import FEATURES, Turn, features
from app.domain import RoundPhase

MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "conductor.joblib"

SPEAK_THRESHOLD = 0.7  # act only when the model is fairly sure the chat is going quiet
MIN_DISCUSSION_S = 20  # let a reveal land before considering anything
MAX_DISCUSSION_S = 360  # never stall a game forever
ANSWER_TIMEOUT_S = 90  # reveal even if someone never answers
MAX_NUDGES_PER_ROUND = 1
NUDGE_GRACE_S = 30  # after nudging someone, give them time to answer before moving on


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
    def __init__(self, path: Path = MODEL_PATH):
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
        return 1 / (1 + math.exp(-(x[0] - 45) / 10)), x

    def decide(self, state: RoomState) -> Decision:
        if state.phase == RoundPhase.ANSWERING:
            if state.member_ids <= state.responded_ids:
                return Decision(Action.REVEAL, "everyone answered")
            if state.now - state.round_opened_at >= ANSWER_TIMEOUT_S:
                return Decision(Action.REVEAL, "answer time ran out")
            waiting = len(state.member_ids - state.responded_ids)
            return Decision(Action.WAIT, f"waiting on {waiting} answer{'s' if waiting != 1 else ''}")

        if state.phase != RoundPhase.REVEALED or state.revealed_at is None:
            return Decision(Action.WAIT, "no round in discussion")

        since_reveal = state.now - state.revealed_at
        if since_reveal < MIN_DISCUSSION_S:
            return Decision(Action.WAIT, "letting the reveal land")

        p, x = self.p_silence(list(state.turns), state.now, is_group=len(state.member_ids) > 2)
        detail = dict(zip(FEATURES, x, strict=True))
        if since_reveal >= MAX_DISCUSSION_S:
            return Decision(Action.NEXT_ROUND, "discussion hit the time cap", p, None, self.source, detail)
        if p < SPEAK_THRESHOLD:
            return Decision(Action.WAIT, "conversation is still going", p, None, self.source, detail)
        if state.last_nudge_at is not None and state.now - state.last_nudge_at < NUDGE_GRACE_S:
            return Decision(Action.WAIT, "giving the nudged player time to answer", p, None, self.source, detail)

        last_speaker = state.turns[-1].sender if state.turns else None
        can_nudge = (
            state.nudges_this_round < MAX_NUDGES_PER_ROUND
            and state.story_holder_id is not None
            and state.story_holder_id != last_speaker
        )
        if can_nudge:
            return Decision(Action.NUDGE, "going quiet and someone has an untold story", p,
                            state.story_holder_id, self.source, detail)
        return Decision(Action.NEXT_ROUND, "conversation has wound down", p, None, self.source, detail)
