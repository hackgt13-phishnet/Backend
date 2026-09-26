"""Group memory: cluster a group's shared content into moments and inside jokes.

Pure functions over precomputed embeddings, so the API server never loads an embedding model.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer

MOMENT_NAMESPACE = uuid.UUID("0b6d8f4e-2c1a-4e7b-9d3f-5a6b7c8d9e0f")

# A topic that keeps returning across this many days and calendar months is an inside joke.
INSIDE_JOKE_MIN_SPAN_DAYS = 60
INSIDE_JOKE_MIN_MONTHS = 3

# Chat filler that TF-IDF would otherwise treat as distinctive.
CHAT_STOP_WORDS = [
    "im", "ur", "u", "bro", "lol", "lmao", "lmaooo", "fr", "rn", "istg", "deadass", "gonna", "ya",
    "yall", "just", "like", "literally", "actually", "really", "abt", "pls", "dont", "doesn", "didn",
    "wait", "ok", "okay", "stop", "said", "got", "gc", "hasn", "ain",
]


@dataclass(frozen=True)
class Item:
    id: str
    body: str
    occurred_at: datetime
    sender_profile_id: str
    participant_profile_ids: tuple[str, ...]


@dataclass
class Moment:
    id: str
    kind: str  # "moment" or "inside_joke"
    item_ids: list[str]
    participant_profile_ids: list[str]
    first_at: datetime
    last_at: datetime
    centroid: list[float]
    label: str = ""
    keywords: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.item_ids)


def normalize(embeddings: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings / np.clip(norms, 1e-12, None)


def cluster(embeddings: np.ndarray, min_cluster_size: int = 5, min_samples: int = 2) -> np.ndarray:
    """HDBSCAN over unit vectors, so Euclidean distance tracks cosine similarity. -1 marks noise."""
    return HDBSCAN(min_cluster_size=min_cluster_size, min_samples=min_samples, copy=True).fit_predict(
        normalize(embeddings)
    )


def classify_kind(times: list[datetime]) -> str:
    span_days = (max(times) - min(times)).days
    months = {(t.year, t.month) for t in times}
    if span_days >= INSIDE_JOKE_MIN_SPAN_DAYS and len(months) >= INSIDE_JOKE_MIN_MONTHS:
        return "inside_joke"
    return "moment"


def keywords(texts: list[str], top_k: int = 3) -> list[str]:
    """Most distinctive terms in a cluster, used as a fallback name when the LLM is unavailable."""
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

    vectorizer = TfidfVectorizer(
        stop_words=sorted(ENGLISH_STOP_WORDS.union(CHAT_STOP_WORDS)),
        ngram_range=(1, 2),
        token_pattern=r"(?u)\b[a-z][a-z]+\b",
    )
    try:
        matrix = vectorizer.fit_transform([t.lower() for t in texts])
    except ValueError:
        return []
    scores = np.asarray(matrix.sum(axis=0)).ravel()
    terms = vectorizer.get_feature_names_out()
    picked: list[str] = []
    for index in scores.argsort()[::-1]:
        term = terms[index]
        if any(term in p or p in term for p in picked):
            continue
        picked.append(term)
        if len(picked) == top_k:
            break
    return picked


def build_moments(items: list[Item], embeddings: np.ndarray, labels: np.ndarray) -> list[Moment]:
    moments = []
    unit = normalize(embeddings)
    for label in sorted(set(labels.tolist()) - {-1}):
        members = [i for i, lab in enumerate(labels) if lab == label]
        group = [items[i] for i in members]
        times = [it.occurred_at for it in group]
        centroid = normalize(unit[members].mean(axis=0, keepdims=True))[0]
        item_ids = sorted(it.id for it in group)
        moments.append(
            Moment(
                id=str(uuid.uuid5(MOMENT_NAMESPACE, ",".join(item_ids))),
                kind=classify_kind(times),
                item_ids=item_ids,
                participant_profile_ids=sorted({p for it in group for p in it.participant_profile_ids}),
                first_at=min(times),
                last_at=max(times),
                centroid=centroid.round(6).tolist(),
                keywords=keywords([it.body for it in group]),
            )
        )
    return sorted(moments, key=lambda m: m.first_at)
