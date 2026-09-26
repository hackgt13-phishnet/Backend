from datetime import UTC, datetime, timedelta

import numpy as np

from app.ai.memory import Item, build_moments, classify_kind, cluster, keywords
from app.ai.naming import fallback_name, valid_name

START = datetime(2025, 7, 1, tzinfo=UTC)


def blob(rng, center, n, spread=0.05):
    return center + rng.normal(0, spread, size=(n, center.shape[0]))


def make_items(n, start, step):
    return [
        Item(id=f"item-{start:%j}-{k}", body=f"text {k}", occurred_at=start + step * k,
             sender_profile_id="a", participant_profile_ids=("a", "b"))
        for k in range(n)
    ]


def test_cluster_separates_topics_and_leaves_noise():
    rng = np.random.default_rng(0)
    dim = 16
    a, b = np.eye(dim)[0], np.eye(dim)[1]
    noise = rng.normal(0, 1, size=(6, dim))
    vectors = np.vstack([blob(rng, a, 10), blob(rng, b, 10), noise])
    labels = cluster(vectors, min_cluster_size=5)
    assert len(set(labels[:10])) == 1 and labels[0] != -1
    assert len(set(labels[10:20])) == 1 and labels[10] != -1
    assert labels[0] != labels[10]


def test_short_burst_is_a_moment():
    times = [START + timedelta(days=d) for d in range(7)]
    assert classify_kind(times) == "moment"


def test_topic_returning_over_months_is_an_inside_joke():
    times = [START + timedelta(days=30 * m) for m in range(5)]
    assert classify_kind(times) == "inside_joke"


def test_long_span_in_two_months_is_not_an_inside_joke():
    times = [START, START + timedelta(days=61)]
    assert classify_kind(times) == "moment"


def test_build_moments_groups_items_and_participants():
    rng = np.random.default_rng(1)
    dim = 8
    items = make_items(6, START, timedelta(days=1)) + make_items(6, START + timedelta(days=200), timedelta(days=40))
    vectors = np.vstack([blob(rng, np.eye(dim)[0], 6), blob(rng, np.eye(dim)[1], 6)])
    labels = np.array([0] * 6 + [1] * 6)
    moments = build_moments(items, vectors, labels)
    assert [m.size for m in moments] == [6, 6]
    assert moments[0].kind == "moment"
    assert moments[1].kind == "inside_joke"
    assert moments[0].participant_profile_ids == ["a", "b"]
    assert abs(np.linalg.norm(moments[0].centroid) - 1) < 1e-3


def test_keywords_pick_the_distinctive_terms():
    texts = ["kofi's rice cooker has aura", "you don't leave the rice cooker behind", "rice cooker slander"]
    assert "rice cooker" in keywords(texts)


def test_name_validation_and_fallback():
    assert valid_name("the 3am fire alarm")
    assert not valid_name("fire")
    assert not valid_name(None)
    assert fallback_name(["rice cooker", "kofi"]) == "rice cooker · kofi"
    assert fallback_name([]) == "untitled moment"
