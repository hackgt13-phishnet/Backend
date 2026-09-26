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

**Status:** built and unit-tested (26 tests). `scripts/demo_rounds.py` plays the AI side offline: it picks a split moment for Who Sent This? (Mario Kart: Ana, Kofi, Dev 92%; Maya, Sam ~20%) and a shared one for Most Likely To (the rice cooker). The answer to Who Sent This? always comes from the data, never the model. Muse output that breaks a rule falls back to a template, and every round records `written_by`. Tested live against Muse (`muse-spark-1.3`): every round, nudge and moment name was written by Muse. With `reasoning_effort=minimal`, each call takes about 3-4s (10-17s at the default effort). Not yet run against a live Supabase project.

## Phase 4: Interest path (`ai/interests`)

For rooms with little or no shared history, and to let the material decide each round's type.

- **Muse reads each player's own activity** (posts, stories, liked reels, saves, follows) into up to 6 interests, each citing the items it came from. Interests learned only from private signals (likes, saves, follows) are passed on as a bare topic and never quoted. Cached per player until their activity changes.
- **Muse matches interests across players:** overlaps (same thing, worded differently) and clashes (same topic, opposite sides). Every match must cite a real interest index for each player, or it's dropped.
- **Planner** picks the flowchart branch from playable moments: a lot (3+), some, little or none, nothing usable. It then chooses each round from all the material, with scores that are comparable across round types: guessing rounds need a split room, vote rounds need the least-informed player to know it, clashes become Hot Takes, overlaps become This or That, and one player's public interest becomes a room-wide This or That. It rewards variety, never reuses an interest, and rotates the spotlight. With nothing usable, it falls back to general rounds.
- New round types: `hot_take` (agree/disagree) and `this_or_that`.

**Status:** live against Muse (`scripts/demo_interests.py`). Dev + Riya, no shared history: an F1 rivalry Hot Take, then Dev's GNX and Riya's Jujutsu Kaisen as room-wide This or That rounds. All five friends: a mix of Most Likely To and Who Sent This?. Add Riya to that group and she gets guessing rounds plus the F1 Hot Take instead of rice-cooker trivia. Every Muse call now retries once and logs failures (a dropped call had silently sent a room down the wrong branch). Not run against Supabase.

## Demo pace and quality pass

- **`GAME_PACE=demo`:** a second conductor trained to predict 20s of silence instead of 60s (`train_conductor.py --window 20`). Its threshold (0.5) was picked from held-out data: right 76% of the time it acts, catching 85% of quiet moments. It never speaks within 8s of a message. A full 3-round, 5-player game takes about 3.5 minutes (normal pace: about 7).
- **Grounded interest rounds:** the writer gets the players' actual specifics (without names), must mention one, and retries once with a reason-specific correction. Judged by a separate Muse call on the same cached inputs: on topic 67% → 100% (4 runs).
- **Nudges that know the round:** opinion rounds turn to whoever was outvoted, about their own pick; Most Likely To turns to who the group picked; Who Sent This? to the sender. Worth answering: 0% → 80–100% (3 runs). Remaining misses: guessing what a bare "disagree" meant.
- `scripts/eval_quality.py` re-runs the comparison.

## Stretch (only after all three phases work)

Bandit round picker, image embeddings (MetaCLIP), Llama Guard, a trained knowledge map.

## How it connects to BackendPlan.md

| BackendPlan.md | What's built |
|---|---|
| `POST /v1/rooms/{id}/sessions`, host only | Built. Picks 3 moments, has Muse write all 3 rounds in parallel (~4s), opens round 1, keeps 2 and 3 pending |
| `POST /v1/rounds/{id}/responses` | Built. One immutable answer per player, must be one of the options. Posts a `submission_status` event |
| `POST /v1/rounds/{id}/reveal`, `/advance`, host only | Built as **host overrides only**. The AI game master reveals once everyone has answered, and advances when the conductor predicts the chat is winding down |
| `GET /v1/rooms/{id}`, `GET /v1/rooms/{id}/timeline` | Built, for bootstrapping after launch or reconnect |
| Responses hidden until reveal | Answers live in `round_answers` (RLS on, no policies, not in Realtime). They reach clients only inside the `game_reveal` event |
| Muse behind an `LLMClient` interface, fallback rounds on failure | `app/ai/llm.py` tries Muse and then any OpenAI-compatible fallback. Every round and line is validated, with a template fallback. `written_by` records which one |
| Deterministic retrieval (3-8 items) | Replaced by the ML picker: moments from embeddings + HDBSCAN, and a knowledge map that picks split moments for Who Sent This? and shared ones for Most Likely To |

**For the app:** subscribe to `timeline_events` for the room. Event types: `message`, `game_started`, `game_prompt` (prompt, quote, options), `submission_status` (answered/total), `game_reveal` (answer, votes, reveal line, or `game_over`), and `host_line` (the game master's nudge: text + target). `GET /v1/rooms/{id}/gm-decisions` feeds the judges' debug view.

**Decided:** the AI is the game master. It reveals and advances on its own, and the host's reveal/advance endpoints are only overrides. This replaces BackendPlan.md's host-controlled flow, which is out of date on this point.
