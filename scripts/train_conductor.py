"""Train the conductor: P(the chat goes quiet in the next 60 seconds).

Decision points are sampled a few seconds after each message in an active conversation.
Cross-validation is grouped by thread, so every score is on conversations the model never saw.

  uv run python scripts/train_conductor.py            # reads data/private/timing.csv
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.conductor_features import FEATURES, SILENCE_WINDOW_S, Turn, features

ROOT = Path(__file__).resolve().parent.parent
DECISION_OFFSETS_S = (
    0,
    10,
    20,
    40,
    60,
    90,
    120,
    180,
)  # when the game master might check, in s after a message
DEMO_OFFSETS_S = (
    0,
    4,
    8,
    12,
    16,
    20,
    30,
    45,
    60,
)  # the demo pace checks every 2s and acts on a 20s window
ACTIVE_GAP_S = 600  # only learn from messages that are part of an active conversation


def load_threads(path: Path) -> dict[str, tuple[list[Turn], bool]]:
    threads: dict[str, list[Turn]] = defaultdict(list)
    groups: dict[str, bool] = {}
    with path.open() as f:
        for row in csv.DictReader(f):
            threads[row["thread"]].append(
                Turn(
                    ts=int(row["ts_ms"]) / 1000,
                    sender=row["sender"],
                    length=int(row["length"]),
                    is_question=row["is_question"] == "1",
                    has_media=row["has_media"] == "1",
                )
            )
            groups[row["thread"]] = row["is_group"] == "1"
    return {k: (sorted(v, key=lambda t: t.ts), groups[k]) for k, v in threads.items()}


def samples(
    threads: dict[str, tuple[list[Turn], bool]],
    window: float = SILENCE_WINDOW_S,
    offsets: tuple[float, ...] = DECISION_OFFSETS_S,
):
    X, y, group = [], [], []
    for thread, (turns, is_group) in threads.items():
        for i in range(1, len(turns) - 1):
            if turns[i].ts - turns[i - 1].ts > ACTIVE_GAP_S:
                continue  # first message of a new conversation, not mid-conversation
            history = turns[: i + 1]
            next_ts = turns[i + 1].ts
            for offset in offsets:
                now = turns[i].ts + offset
                if next_ts <= now:
                    break  # someone already replied before this check
                X.append(features(history, now, is_group))
                y.append(int(next_ts - now > window))
                group.append(thread)
    return np.array(X), np.array(y), np.array(group)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timing", type=Path, default=ROOT / "data" / "private" / "timing.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "models" / "conductor.joblib")
    parser.add_argument(
        "--window",
        type=float,
        default=SILENCE_WINDOW_S,
        help="seconds of silence that count as 'going quiet' (60 normal, 20 demo)",
    )
    args = parser.parse_args()

    offsets = DECISION_OFFSETS_S if args.window >= SILENCE_WINDOW_S else DEMO_OFFSETS_S
    X, y, group = samples(load_threads(args.timing), args.window, offsets)
    print(f"{len(y)} decision points from {len(set(group))} threads · {y.mean():.0%} go quiet")

    def make_model() -> HistGradientBoostingClassifier:
        return HistGradientBoostingClassifier(
            max_iter=300,
            learning_rate=0.05,
            max_leaf_nodes=31,
            l2_regularization=1.0,
            random_state=7,
        )

    # Baseline: "the longer since the last message, the quieter it is." The model has to beat this.
    # "Mid-conversation" = checks within 40s of the last message, where timing alone says least.
    since = X[:, FEATURES.index("secs_since_last")]
    aucs, baselines, mid_aucs, mid_baselines = [], [], [], []
    for train, test in GroupKFold(n_splits=5).split(X, y, group):
        model = make_model().fit(X[train], y[train])
        p = model.predict_proba(X[test])[:, 1]
        mid = since[test] <= 40
        aucs.append(roc_auc_score(y[test], p))
        baselines.append(roc_auc_score(y[test], since[test]))
        mid_aucs.append(roc_auc_score(y[test][mid], p[mid]))
        mid_baselines.append(roc_auc_score(y[test][mid], since[test][mid]))
    auc, baseline = float(np.mean(aucs)), float(np.mean(baselines))
    mid_auc, mid_baseline = float(np.mean(mid_aucs)), float(np.mean(mid_baselines))
    print(
        f"5-fold CV by thread · AUC {auc:.3f} ± {np.std(aucs):.3f} "
        f"(baseline, time since last message: {baseline:.3f} ± {np.std(baselines):.3f})"
    )
    print(
        f"mid-conversation only · AUC {mid_auc:.3f} ± {np.std(mid_aucs):.3f} "
        f"(baseline: {mid_baseline:.3f} ± {np.std(mid_baselines):.3f})"
    )

    model = make_model()
    model.fit(X, y)  # final model uses every thread
    args.out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {"model": model, "features": FEATURES, "auc": auc, "baseline_auc": baseline}, args.out
    )
    meta = args.out.with_suffix(".json")
    meta.write_text(
        json.dumps(
            {
                "features": FEATURES,
                "silence_window_s": args.window,
                "decision_points": len(y),
                "threads": len(set(group)),
                "positive_rate": round(float(y.mean()), 3),
                "cv_auc": round(auc, 3),
                "baseline_auc": round(baseline, 3),
                "mid_conversation_auc": round(mid_auc, 3),
                "mid_conversation_baseline_auc": round(mid_baseline, 3),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"saved {args.out} and {meta}")


if __name__ == "__main__":
    main()
