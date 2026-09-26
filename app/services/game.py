from app.domain import RoundPhase


ALLOWED_TRANSITIONS: dict[RoundPhase, set[RoundPhase]] = {
    RoundPhase.PENDING: {RoundPhase.ANSWERING},
    RoundPhase.ANSWERING: {RoundPhase.REVEALED},
    RoundPhase.REVEALED: {RoundPhase.COMPLETE},
    RoundPhase.COMPLETE: set(),
}


def can_transition(current: RoundPhase, target: RoundPhase) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: RoundPhase, target: RoundPhase) -> None:
    if not can_transition(current, target):
        raise ValueError(f"Cannot transition round from {current} to {target}")
