"""Conversation-energy features. Shared by training and serving so they can never drift apart."""

from dataclasses import dataclass
from itertools import pairwise

import numpy as np

# The chat counts as "going quiet" if nobody sends anything in this window.
SILENCE_WINDOW_S = 60

FEATURES = [
    "secs_since_last",
    "gap_1",
    "gap_2",
    "gap_3",
    "mean_gap_5",
    "msgs_last_60s",
    "msgs_last_300s",
    "senders_last_5",
    "switches_last_5",
    "last_length",
    "mean_length_5",
    "last_is_question",
    "last_has_media",
    "is_group",
]


@dataclass(frozen=True)
class Turn:
    ts: float  # seconds
    sender: str | int
    length: int
    is_question: bool
    has_media: bool


def features(history: list[Turn], now: float, is_group: bool) -> list[float]:
    """Features at time `now`, using only turns at or before `now`."""
    past = [t for t in history if t.ts <= now]
    if not past:
        return [now, *([0.0] * (len(FEATURES) - 1))]
    last5 = past[-5:]
    gaps = [b.ts - a.ts for a, b in zip(past[-6:], past[-5:], strict=False)][::-1]  # most recent first
    padded = (gaps + [3600.0] * 3)[:3]
    switches = sum(1 for a, b in pairwise(last5) if a.sender != b.sender)
    last = past[-1]
    return [
        now - last.ts,
        *padded,
        float(np.mean(gaps)) if gaps else 3600.0,
        float(sum(1 for t in past if now - t.ts <= 60)),
        float(sum(1 for t in past if now - t.ts <= 300)),
        float(len({t.sender for t in last5})),
        float(switches),
        float(last.length),
        float(np.mean([t.length for t in last5])),
        float(last.is_question),
        float(last.has_media),
        float(is_group),
    ]
