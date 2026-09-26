# AI integration MVP

Three parts, built in three phases. Each phase ships on its own branch with tests.

## Phase 1: Group memory (`ai/group-memory`)

Turn the group's shared content into **moments** the game can build rounds from.

- Embed every item's text (message body or caption) with `all-MiniLM-L6-v2` (384 dimensions). This runs offline in a script, so the API server never loads a model.
- Cluster the embeddings with HDBSCAN. Each cluster is a moment. Items that don't fit any cluster are noise and are never used.
- Label each moment as a `moment` (one burst of activity) or an `inside_joke` (the same topic returning across 60+ days and 2+ months).
- Name each moment with Muse. If Muse isn't configured or fails, fall back to TF-IDF keywords.
- Store the embeddings in pgvector (`group_context_items.embedding`) and the moments in a `moments` table. Row-level security is on with no policies, so only the backend can read them.
- Seed data: a deterministic generator for a 5-person friend group with planted moments (a trip, one chaotic night, finals week) and planted inside jokes (a running bit, a recurring rivalry), so we can test that the clustering finds them.

**Done when:** `uv run python scripts/build_memory.py` finds every planted moment and inside joke in the seed data, writes `seed/moments.json`, and the unit tests pass with no model download.

**Status:** all 5 planted themes found as separate clusters with the right kind. About 70% of themed messages get clustered; the rest never mention the topic.

## Phase 2: Conductor (`ai/conductor`)

Predict whether the chat is about to go quiet, and decide what the game master does.

- `scripts/extract_timing.py`: reads Instagram exports and writes **timing only**: hashed thread ID, timestamp, sender index, message length, whether it's a question, whether it has media. No message text.
- Features over a sliding window: gaps since recent messages, speaker changes, number of distinct speakers, message length, whether the last message is a question.
- Label: no message within the next 60 seconds.
- Model: scikit-learn `HistGradientBoostingClassifier`. Train/test split by thread, report ROC AUC, save to `models/conductor.joblib`.
- `app/ai/conductor.py`: `p_silence(room)` plus a decision policy (WAIT / REVEAL / NUDGE / NEXT_ROUND) with the speak-gate rules. Every decision is logged to `gm_decisions`.
- An asyncio loop checks active rooms every ~5 seconds, plus a check on every new message.

**Done when:** AUC is reported on held-out threads, and the decision loop runs against a seeded room.

**Status:** trained on 15k messages from 188 real threads (timing only). 5-fold CV grouped by thread: AUC **0.823** (baseline, time since last message: 0.785). Mid-conversation (within 40s of the last message), where timing alone says least: AUC **0.769** vs 0.693 baseline. In `scripts/simulate_conductor.py` it stays silent through a lively reveal and first acts 53s after the room goes quiet. The model file (`models/*.joblib`) is gitignored because it's trained on private chats. Run `extract_timing.py` + `train_conductor.py` on your own export to rebuild it. Without it, the conductor falls back to the time-since-last-message baseline.

## Phase 3: Muse rounds (`ai/muse-rounds`)

Generate rounds and nudges from moments, safely.

- Round generation for Who Sent This? and Most Likely To, from a chosen moment, as Pydantic-validated JSON with one retry.
- **Answers are stored in a table clients can't read or subscribe to** (fixes the answer leak from the first commit).
- Choosing a moment: prefer ones the room is split on (some members were in the thread, some weren't), using embedding similarity + participation.
- Nudge lines: only when the conductor allows, at most one per reveal, must name a player, no facts from outside the round (rule-based checks).
- Pre-write the next round during the discussion phase.

**Done when:** a seeded room plays three AI-generated rounds end to end and the game master speaks only when allowed.

## Stretch (only after all three phases work)

Bandit round picker, image embeddings (MetaCLIP), Llama Guard, a trained knowledge map.
