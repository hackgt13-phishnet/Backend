"""Knowledge map + moment picker: choose the moment the room is most split on.

P(member knows a moment) comes from two signals:
- participation: the share of the moment's messages whose thread they were in
- similarity: how close the moment is to what that member usually posts about (embeddings)
Who Sent This? wants a moment the room is split on; Most Likely To wants one everyone shares.
"""

from dataclasses import dataclass

import numpy as np

KNOWN_IF_PARTICIPANT = 0.92
BASE_RATE = 0.08  # chance an outsider heard about it anyway
SIMILARITY_WEIGHT = 0.5  # how much shared interests raise that chance


@dataclass(frozen=True)
class MomentView:
    id: str
    kind: str
    item_ids: tuple[str, ...]
    participant_ids: frozenset[str]
    centroid: np.ndarray


@dataclass(frozen=True)
class ItemView:
    id: str
    sender_id: str
    participant_ids: frozenset[str]
    body: str


@dataclass(frozen=True)
class Pick:
    moment: MomentView
    items: tuple[ItemView, ...]
    p_known: dict[str, float]
    split: float  # 0 = everyone agrees on who knows, 0.25 = perfectly split


def eligible_items(moment: MomentView, items: dict[str, ItemView], members: frozenset[str]) -> list[ItemView]:
    """Consent rule: only items whose whole thread is in the room, sent by someone who's playing."""
    return [
        items[i] for i in moment.item_ids
        if i in items and items[i].sender_id in members and items[i].participant_ids <= members
    ]


def p_known(moment_items: list[ItemView], member: str, vector: np.ndarray | None, centroid: np.ndarray) -> float:
    share = float(np.mean([member in i.participant_ids for i in moment_items])) if moment_items else 0.0
    similarity = float(np.clip(vector @ centroid, 0, 1)) if vector is not None else 0.0
    heard_anyway = BASE_RATE + SIMILARITY_WEIGHT * similarity**2
    return min(0.97, share * KNOWN_IF_PARTICIPANT + (1 - share) * heard_anyway)


def split_score(probabilities: list[float]) -> float:
    """Mean p(1-p): highest when the room is divided on who knows it."""
    return float(np.mean([p * (1 - p) for p in probabilities])) if probabilities else 0.0


def score_moments(
    moments: list[MomentView],
    items: dict[str, ItemView],
    members: frozenset[str],
    member_vectors: dict[str, np.ndarray],
    used_moment_ids: frozenset[str] = frozenset(),
    min_items: int = 2,
) -> list[Pick]:
    """Every moment this room is allowed to play, with who probably knows it."""
    picks = []
    for moment in moments:
        if moment.id in used_moment_ids:
            continue
        usable = eligible_items(moment, items, members)
        if len(usable) < min_items:
            continue
        probabilities = {m: p_known(usable, m, member_vectors.get(m), moment.centroid) for m in members}
        picks.append(Pick(moment, tuple(usable), probabilities, split_score(list(probabilities.values()))))
    return picks


def pick_moment(
    moments: list[MomentView],
    items: dict[str, ItemView],
    members: frozenset[str],
    member_vectors: dict[str, np.ndarray],
    used_moment_ids: frozenset[str] = frozenset(),
    min_items: int = 2,
    want: str = "split",  # "split" for guessing games, "shared" for vote games
) -> Pick | None:
    best: Pick | None = None
    best_score = -1.0
    for moment in moments:
        if moment.id in used_moment_ids:
            continue
        usable = eligible_items(moment, items, members)
        if len(usable) < min_items:
            continue
        probabilities = {m: p_known(usable, m, member_vectors.get(m), moment.centroid) for m in members}
        candidate = Pick(moment, tuple(usable), probabilities, split_score(list(probabilities.values())))
        score = candidate.split if want == "split" else float(np.mean(list(probabilities.values())))
        if best is None or score > best_score:
            best, best_score = candidate, score
    return best


def member_vectors_from_items(item_vectors: dict[str, np.ndarray], items: dict[str, ItemView]) -> dict[str, np.ndarray]:
    """Each member's interests = the normalized mean of everything they've sent."""
    sums: dict[str, list[np.ndarray]] = {}
    for item_id, vector in item_vectors.items():
        item = items.get(item_id)
        if item:
            sums.setdefault(item.sender_id, []).append(vector)
    out = {}
    for member, vectors in sums.items():
        mean = np.mean(vectors, axis=0)
        out[member] = mean / max(np.linalg.norm(mean), 1e-12)
    return out
