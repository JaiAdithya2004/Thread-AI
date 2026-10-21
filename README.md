# Thread

**An AI memory layer for software teams.** Thread connects to the tools a team already uses, extracts structured facts from their conversations and documents, and answers questions with citations back to the source — through a web app, an MCP server, a Slack bot, or a REST API.

The problem it solves: the answer to "why did we choose Postgres over DynamoDB?" exists, but it's in a Slack thread from eight months ago that nobody can find. Thread makes that institutional knowledge queryable.

![Thread](Threadwebsite.png)

---

## Contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Retrieval pipeline](#retrieval-pipeline)
- [What "memory layer" means here](#what-memory-layer-means-here)
- [Keeping connector data fresh](#keeping-connector-data-fresh)
- [Security model](#security-model)
- [Evaluation](#evaluation)
- [Performance](#performance)
- [Testing and CI](#testing-and-ci)
- [Running locally](#running-locally)
- [Project layout](#project-layout)
- [Engineering decisions](#engineering-decisions)
- [Current limitations](#current-limitations)

---

## How it works

```
Sources                 Ingestion              Storage              Retrieval
─────────               ─────────              ───────              ─────────
Slack       ┐          ┌──────────┐         ┌──────────┐         ┌──────────────┐
Gmail       │          │ dedup by │         │ episodes │         │ vector (HNSW)│
Notion      ├─────────▶│ content  ├────────▶│          │────────▶│      +       │
Drive       │          │  hash    │         │  facts   │         │ full-text    │
1000+ apps  ┘          └────┬─────┘         │ (pgvector│         │      ↓       │
                            │               │  + FTS)  │         │     RRF      │
                            ▼               └──────────┘         │      ↓       │
                       ┌──────────┐                              │ ACL + team   │
                       │   LLM    │                              │   filter     │
                       │ extract  │                              │      ↓       │
                       │  facts   │                              │  synthesis   │
                       └──────────┘                              └──────┬───────┘
                                                                        ▼
                                                              cited answer + ledger
```

1. **Ingest** — connectors pull raw *episodes* (messages, docs, threads). Each is hashed on content and deduplicated, so re-syncing a source never duplicates history.
2. **Extract** — an LLM turns each episode into structured *facts* (`subject / predicate / object`, a one-line summary, and a confidence score). Facts carry an ACL and a team.
3. **Reconcile** — before a fact is saved, the nearest existing facts in the same team are found by embedding and an LLM judge classifies each as a duplicate, a replacement (the old fact is marked stale and linked), a conflict, or unrelated.
4. **Retrieve** — a query runs through hybrid search, gets access-filtered, and is synthesized into an answer that cites the exact fact IDs used.
5. **Log** — every search writes to an append-only ledger: who asked, what they asked, and which facts came back.

---

## Architecture

**Single-package core, multiple surfaces.** All product logic lives in `brain/`, and every entry point is a thin wrapper over the same `Brain` class. Adding a surface means writing a wrapper, not reimplementing retrieval.

```
                    ┌──────────────────────────────────┐
   Web app  ───────▶│                                  │
   MCP server ─────▶│      brain.engine.Brain          │
   Slack bot ──────▶│   (single public API surface)    │
   REST API ───────▶│                                  │
   CLI ────────────▶└────────────────┬─────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
       ┌────────────┐         ┌────────────┐         ┌────────────┐
       │  retrieve  │         │  extract   │         │   ingest   │
       └─────┬──────┘         └─────┬──────┘         └─────┬──────┘
             │                      │                      │
             └──────────┬───────────┴──────────┬───────────┘
                        ▼                      ▼
                 ┌────────────┐         ┌────────────┐
                 │   llm.py   │         │   db.py    │
                 │ (metered,  │         │ (Postgres  │
                 │  1 choke   │         │ + pgvector)│
                 │   point)   │         └────────────┘
                 └────────────┘
```

Two constraints hold this together:

- **Every LLM call goes through `llm.py`.** One choke point means token accounting, cost metering, circuit breaking, and BYO-key routing are implemented once rather than at each call site.
- **Every database call goes through `db.py`.** Row-level security session variables are set in one place, so no query can accidentally skip tenant scoping.

**Stack:** FastAPI · Postgres 16 + pgvector · Redis · LiteLLM · Gemini embeddings (768-dim, with an OpenRouter fallback to the same model) · Alembic · arq

---

## Retrieval pipeline

Hybrid retrieval, because vector search and keyword search fail differently. Embeddings miss exact identifiers (an error code, a service name); full-text misses paraphrase. Running both and fusing the rankings covers both failure modes.

| Stage | What happens |
|---|---|
| **1. Embed** | Query → 768-dim vector. In-process cache avoids repeat API round-trips. |
| **2. Cache** | Redis lookup keyed on the embedding **plus workspace, principals, team and flags** — the scope is part of the key, so one tenant can never be served another's cached results. Falls through silently if Redis is down. |
| **3. Vector search** | pgvector, cosine distance. The HNSW index is built out-of-band by `scripts/rebuild_index.py` (index creation locks the table, so it is deliberately not part of migration); the app warns and falls back to sequential scan if it is missing. |
| **4. Full-text search** | Postgres `tsvector`, generated column over summary + subject + object. |
| **5. Fuse** | Reciprocal Rank Fusion — `score = Σ 1/(k + rank)`. Items found by both retrievers rank above items found by one. |
| **6. Filter** | ACL check against the caller's principals, then team-visibility. Applied *after* fusion so filtering can't be skipped by a ranking path. |
| **7. Rerank** | Optional cross-encoder rerank of the top 30 (`BRAIN_RERANK`, **off by default**; the benchmark below runs without it). |
| **8. Synthesize** | LLM writes a prose answer citing fact IDs. Below a confidence threshold it abstains rather than guessing. |
| **9. Log** | Append-only ledger write. |

**Graceful degradation is deliberate.** If the LLM is unavailable, step 8 returns the ranked facts without prose rather than erroring — a degraded answer beats no answer. If Redis is down, every cache call is a no-op.

---

## What "memory layer" means here

The phrase is used precisely, because it is easy to overclaim. Thread stores knowledge in three layers:

| Layer | Table | Role |
|---|---|---|
| **Raw source** | `episodes` | The original email, commit or message — full body, author, URL, `content_hash` for dedup |
| **Extracted claims** | `facts` | `subject / predicate / object` + summary + confidence, with `fact_vec_768` and `fts_vec`. **This is the only table retrieval reads.** |
| **Relations** | `fact_edges` | `supersedes` and `in_tension_with` links between facts |

**What is implemented:**

- **Temporal versioning.** Facts carry `introduced_at` / `invalidated_at`. A superseded fact is invalidated rather than deleted, so "what did we decide *before*?" stays answerable and provenance survives. Decision history returns stale facts alongside valid ones, in order.
- **Semantic supersession.** When a fact is saved, `brain/core/reconcile.py` finds the nearest existing facts in the same team by embedding (never showing the judge a private fact the author could not see) and asks an LLM judge to classify each as `duplicate`, `supersedes`, `contradicts` or `unrelated`. Embeddings only find candidates, because similarity cannot tell an update from a different fact: "launch is July 5th" and "July 10th" embed almost identically. If the judge is unavailable it falls back to the exact-subject duplicate check and never invalidates on a guess. See [Evaluation](#evaluation) for how well this works and where it fails.
- **Contradiction surfacing (query path).** Retrieval joins `fact_edges` for `in_tension_with` among the returned facts, so an answer can report that two facts disagree.
- **Content-hash deduplication** on ingest, making re-sync and webhook replay idempotent.
- **Referential integrity.** `fact_edges` cascades on fact deletion, so the graph cannot drift out of sync with the facts it describes.

**What is not implemented — stated plainly:**

- **Future-dated changes are mishandled.** "The price *will* be $89 from January 1" currently invalidates today's price. The benchmark measures this failure (6 of 6) rather than hiding it.
- **Supersession misses about one update in seven** in the benchmark, mostly changes of provider or location rather than of a number.
- **No conversational memory.** `conversations`/`messages` exist as tables but nothing from a prior turn feeds back into retrieval. Every query is stateless; a follow-up does not know what preceded it.
- **No entity or user profiles**, and no consolidation of older facts into higher-level ones.
- **The older conflict heuristic is still string-based.** `in_tension_with` edges written at extraction time compare `subject` + `predicate` exactly, so generic subjects ("The team") can produce false conflicts. The reconcile judge's `contradicts` verdict is the semantic replacement, but the two have not been merged.
- **Each new fact costs one extra short LLM call** when a similar fact already exists.

So: **a versioned fact store whose versions are now decided semantically**, with measured accuracy and documented failure modes — still not an agent memory system in the sense of Mem0 or Zep.

---

## Keeping connector data fresh

Ingestion is designed around three paths, all converging on the same idempotent write:

| Path | Trigger | Purpose |
|---|---|---|
| **Polling** | arq cron, every `BRAIN_POLL_INTERVAL_MINUTES` (default 10) | Steady-state incremental sync |
| **Webhooks** | Provider push → `connector_webhook_routes` | Low-latency updates |
| **Reconciliation** | arq cron, every `BRAIN_RECONCILE_INTERVAL_MINUTES` (default 60) | Catches anything the first two missed |

**Incremental, not full re-sync.** `connector_sync_state` holds a `last_synced_at` watermark per `(workspace, source)`. Each run passes it as `since` and advances it *only on success*, so a failed run re-fetches rather than skipping a window. Failures increment `consecutive_failures` for backoff, `locked_until` prevents overlapping runs of the same source, and jobs exhausting `BRAIN_JOB_MAX_RETRIES` land in `dead_letter_jobs` for inspection rather than vanishing.

Reconciliation deliberately overlaps its window by `job_timeout × max_tries × 2`, on the assumption that a missed episode is worse than a duplicate one — content-hash dedup makes the duplicate free.

### Two ways to drive it

The sync logic is the same either way; what differs is what schedules it.

**Scheduled one-shot (default, no infrastructure).** `scripts/sync_once.py` performs a single incremental pass and exits — no queue, no daemon, no Redis. `.github/workflows/sync.yml` runs it every 15 minutes on GitHub Actions. This is the deployed configuration, because it needs no always-on host and works alongside a serverless web tier.

```bash
python scripts/sync_once.py                    # every workspace
python scripts/sync_once.py --workspace ws_demo
python scripts/sync_once.py --dry-run
```

**Long-running worker (optional).** `arq brain.worker.WorkerSettings` adds webhook ingestion and per-job retry backoff, and needs Redis plus a host that keeps a process alive. Use it when second-level freshness matters more than deployment simplicity.

Both read and advance the same `connector_sync_state` watermark, so they interoperate — whichever runs last moves the mark forward. Running both is safe but redundant.

**Required environment:** `DATABASE_URL`, `COMPOSIO_API_KEY`, `GEMINI_API_KEY`, and `BRAIN_WORKSPACE`. For GitHub Actions, add these under *Settings → Secrets and variables → Actions*; the workflow fails fast with a named list if any are missing.

---

## Security model

Multi-tenancy is enforced in the database, not only in application code — a bug in a handler shouldn't be able to leak another tenant's data.

| Control | Implementation |
|---|---|
| **Tenant isolation** | Postgres row-level security on every tenant table (`episodes`, `facts`, `fact_edges`, `ledger`, `teams`), keyed on a session variable. |
| **Private chat threads** | `conversations` and `messages` use **FORCE** row-level security and drop to a non-`BYPASSRLS` role, so not even the table owner can read another user's threads. Admins cannot read employee chats. |
| **Identity** | Always derived server-side from the authenticated session. Never read from a request body, query string, or header. |
| **Fact-level access** | Each fact carries an ACL and a team; visibility is `team` or `shared`. |
| **Secrets** | Platform keys are loaded from environment only — never stored in the database, logged, or returned in a response. |
| **Customer keys at rest** | Customer-supplied LLM keys are Fernet-encrypted (AES-128-CBC + HMAC-SHA256) before they reach the database, under a master key held in `BRAIN_KEY_ENCRYPTION_KEY`. Decryption fails closed: a missing or wrong master key yields an empty string rather than ciphertext. The read endpoint returns booleans only, never key values. |
| **Platform metrics** | Gated behind a `BRAIN_PLATFORM_OWNER` env flag, not a customer role, so no customer admin can reach cross-tenant figures. |
| **Dev auth bypass** | Refuses to start if enabled while `BRAIN_ENV=production`. |
| **Error responses** | 5xx details are logged server-side and returned opaque, so driver text and schema names never reach a client. |

A dedicated **isolation suite** (`tests/test_isolation.py`) treats these as testable claims — cross-workspace reads, admin-reading-employee-chat, invite token reuse, expired invites, and SQL injection attempts each have a test that must fail loudly if the control regresses.

---

## Evaluation

Quality is measured against explicit ground truth, not asserted. `evals/bench.py` runs a fixed benchmark on a **synthetic company ("Meridian Labs")** so that anyone can reproduce it: there is no personal data in the corpus and nothing to connect.

| | |
|---|---|
| **Knowledge base** | 44 facts |
| **Answerable questions** | 45 (32 direct, 13 deliberately paraphrased with little word overlap), each with the gold fact IDs that must be returned |
| **Unanswerable questions** | 10 (the system should abstain) |
| **Changing facts** | 14 chains, 30 versions, 16 updates (a price rises twice, an owner changes, a vendor is replaced) |
| **Hard negatives** | 14 "decoys" that resemble an existing fact but must replace nothing, 8 paraphrases that should be detected as duplicates, 2 future-dated changes |

Ground truth is written by reading `evals/corpus.yaml`, never derived from what retrieval returned; an earlier version of this harness did that, which made recall impossible to lower, and was withdrawn.

```bash
python -m evals.bench                      # all three parts; needs a LOCAL Postgres and LLM/embedding keys
python -m evals.bench --parts A --no-answers   # retrieval only: no chat model
python -m evals.aggregate                  # combine runs/*.json into the tables below
```

The benchmark deletes and recreates its `ws_bench_*` workspaces, so it refuses to run against a non-local `DATABASE_URL`. If an embedding call fails it aborts instead of scoring zero vectors.

### Results

The LLM-judged parts are not deterministic, so everything below is from **3 full runs** (2026-10-21, on commit `cc9e67c` plus the changes in this release): the mean is shown with the range across runs, and counts are pooled. Raw output is in `evals/results/`.

**Retrieval (k=5)**

| Ranker | Hit@5 | Recall@5 | Precision@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|
| vector | **1.000** (135/135; all runs equal) | **1.000** | **0.209** | **0.982** | **0.986** |
| fulltext | **0.244** (33/135; all runs equal) | **0.244** | **0.049** | **0.244** | **0.244** |
| hybrid_rrf | **1.000** (135/135; all runs equal) | **1.000** | **0.209** | **0.982** | **0.986** |
| production | **1.000** (135/135; all runs equal) | **1.000** | **0.209** | **1.000** | **1.000** |

**Answer quality**

| Metric | Result |
|---|---|
| Answer correct (answerable) | **1.000** (135/135; all runs equal) |
| Faithfulness (LLM judge) | **0.956** (129/135; all runs equal) |
| Citation validity (cited id was retrieved) | **1.000** (143/143; all runs equal) |
| Citation support (LLM judge) | **1.000** (143/143; all runs equal) |
| Abstention on unanswerable | **1.000** (30/30; all runs equal) |
| Answer latency p50 / p95 (s) | 3.9733 / 13.8233 |
| Retrieval latency p50 / p95 (s) | 2.3397 / 3.642 |

**Supersession decisions (pairwise, database in true state)**

| Metric | Exact-subject (previous) | Embedding similarity only | Embedding + LLM judge (current) |
|---|---|---|---|
| Supersession precision | **1.000** | **0.385** (0.35-0.42) | **0.855** (0.83-0.88) |
| Supersession recall | **0.062** (3/48; all runs equal) | **0.625** (30/48; 0.56-0.69) | **0.854** (41/48; 0.75-0.94) |
| Supersession F1 | **0.118** | **0.476** (0.43-0.50) | **0.852** (0.80-0.88) |
| Updates lost as 'duplicates' | **0.000** (0/48; all runs equal) | **0.188** (9/48; 0.06-0.31) | **0.000** (0/48; all runs equal) |
| Paraphrases recognised as duplicates | **0.083** (2/24; 0.00-0.12) | **0.458** (11/24; 0.00-0.75) | **1.000** (24/24; all runs equal) |
| Decoys wrongly acted on | **0.000** (0/42; all runs equal) | **0.714** (30/42; 0.50-1.00) | **0.024** (1/42; 0.00-0.07) |
| Future-dated changes wrongly superseded | **0.000** (0/6; all runs equal) | **1.000** (6/6; all runs equal) | **1.000** (6/6; all runs equal) |
| True predecessor among candidates | - | - | **0.979** (47/48; 0.94-1.00) |

**End to end: changing facts through the real save pipeline**

| Metric | Reconciliation off (previous) | Reconciliation on (current) |
|---|---|---|
| Versions stored | **0.944** (85/90; 0.93-0.97) | **1.000** (90/90; all runs equal) |
| Version status correct (valid vs stale) | **0.411** (37/90; 0.40-0.43) | **0.989** (89/90; 0.97-1.00) |
| Chains fully correct | **0.000** (0/42; all runs equal) | **0.976** (41/42; 0.93-1.00) |
| 'Current X?' top result is the latest | **0.167** (7/42; 0.14-0.21) | **0.976** (41/42; 0.93-1.00) |
| 'Current X?' answer states latest only | **0.000** (0/42; all runs equal) | **0.976** (41/42; 0.93-1.00) |
| History returns every version | **0.944** (85/90; 0.93-0.97) | **1.000** (90/90; all runs equal) |
| History marks stale/valid correctly | **0.411** (37/90; 0.40-0.43) | **0.989** (89/90; 0.97-1.00) |
| Unrelated facts wrongly invalidated | 0 | 0 |


**Models:** extraction and the supersession judge `gemini-2.5-flash-lite`; answers `gemini-3.8-flash`; answer judge `gpt-4o-mini` (a different model family from the answerer, to avoid self-grading); embeddings `gemini-embedding-001`, 768-dim. Per-run cost was not metered; at current OpenRouter prices the token volume is on the order of cents.

### What the numbers show

**Retrieval is solved at this scale, which says little about larger corpora.** Hit@5 is 1.000 on all 135 question-runs (95% Wilson lower bound 0.92 per run). But the corpus has 44 facts and K=5 returns 11% of it on every query, so a ceiling is expected. Hybrid and vector-only are identical on this corpus; full-text alone finds the right fact for only 24% of questions because Postgres `plainto_tsquery` ANDs its terms, so semantic search carries the system. Treat this as evidence of "no defect and working semantics", not of production-scale ranking.

**Answers are grounded.** Every answer contained the expected content (135/135), every cited fact ID was real and supported the sentence (143/143), and all 30 unanswerable questions were refused. Faithfulness is 0.956: the six answers flagged were ones that appended a caveat sentence such as "this is not specified in company memory" — the judge treated the caveat as an unsupported claim. Abstention is detected with a phrase match on the answer, not a judge.

**Supersession is the headline.** Deciding "this new fact replaces that one" with embeddings to find candidates and an LLM to classify them reaches F1 0.85, against 0.12 for the previous exact-subject method and 0.48 for embedding similarity alone. Two failure modes of the alternatives are worth naming:

- *Embedding similarity alone cannot tell an update from a different fact.* Decoys scored 0.69-0.89 similar to the fact they resemble, overlapping the 0.73-0.98 of true updates (measured on the fallback embedding route), so a threshold either misses updates or replaces unrelated facts: it wrongly acted on 71% of decoys, and silently dropped 19% of real updates as "duplicates".
- *The previous method almost never fires.* It needs the extraction model to recall a fact it was never shown and then matches subjects exactly; it replaced the right fact for 3 of 48 updates and recognised 2 of 24 paraphrases as duplicates.

Through the real save pipeline, with reconciliation off, no chain ended up in the correct final state (0 of 42), and "what is the current X?" ranked the latest value first for only 17% of chains (and never produced an answer stating only the latest value). With it on, 41 of 42 chains are correct and the question is answered correctly for 41 of 42. No unrelated fact was ever invalidated.

### Known weaknesses

- **Future-dated changes are mishandled.** "The Team plan *will* cost $89 starting January 1" invalidates today's $79 fact (6/6 in both LLM-based methods). The current value is still true until that date. This is in the benchmark deliberately so it is measured rather than hidden; fixing it needs "effective from" semantics.
- **Seven of 48 updates were missed** (both versions left valid), always the same few: AWS to Google Cloud, React Native to Flutter, Austin to Denver, and once the third step of the Pro plan price. These are changes of provider or location rather than of a number, and the judge did not treat them as replacements.
- **A decoy was superseded in one of three runs:** "The analytics warehouse runs on PostgreSQL 16" replaced "the primary database is PostgreSQL 15".
- **The true predecessor was missing from the candidate set once in 48 updates** (0.979), so a candidate floor of 0.70 is not quite a guarantee.
- **Latency.** Answers take a median of ~4 s and a p95 of ~14 s, and retrieval alone a median of ~2.3 s. Both are dominated by network calls to the embedding and model providers from a development machine; the figures say nothing about a co-located deployment.

### Limits of this evaluation

- The corpus and questions were written by the person who built the system. The 13 paraphrased questions and the decoys are meant to counter that, but an independent set would be stronger.
- Subjects in the changing-fact chains are written to vary the way an extraction model's do, which favours the new method. The previous method is credited with the extraction hint it needs, whenever an update really occurs, so its score is a ceiling, not a typical figure.
- The candidate floor (`RELATED_THRESHOLD` = 0.70) was chosen after looking at this benchmark's pair similarities: 0.75 missed 5 of 16 true updates when embeddings were served by the OpenRouter fallback. It has not been validated on held-out data.
- 3 runs on one machine. Run-to-run spread is reported, but n is small (16 updates, 14 chains), so intervals are wide.
- Embeddings occasionally came from the OpenRouter fallback (same model, no task-type hint) when Gemini rate-limited: one batch in run 1 and 34 in run 3. A separate full run entirely on the fallback route gave candidate recall 16/16 and supersession precision 0.86 and recall 0.75. Its end-to-end answer metrics are excluded because synthesis returned empty responses during that run (`evals/results/bench_fallback_embeddings.json`).

### Quality gate

`tests/test_regression.py` runs the retrieval benchmark against the floors in `evals/thresholds.yaml` and the recorded `evals/baseline.json`. It needs a local Postgres and a `GEMINI_API_KEY` (or `OPENROUTER_API_KEY`) for query embeddings, so CI currently runs only its no-database architecture guards; run the full gate locally with `pytest tests/test_regression.py`. The LLM-judged parts run with `BENCH_LLM=1`. After an intentional change, refresh the baseline with `python -m evals.aggregate --write-baseline`.

| Metric | Hard floor | Tolerance vs baseline |
|---|---|---|
| Retrieval hit@5, recall@5 | 0.90 | 0.05 |
| Retrieval MRR, nDCG@5 | 0.85 | 0.05 |
| Answer correct / faithfulness / citation support | 0.90 / 0.85 / 0.90 | 0.08 |
| Supersession recall / precision | 0.80 / 0.70 | 0.10 |
| Decoys wrongly acted on (ceiling) | 0.20 | 0.10 |
| Chains fully correct; current-answer correctness | 0.70; 0.75 | 0.15 |

`evals/run.py` and `evals/questions.yaml` remain as a harness for scoring your own workspace's questions with keyword-based relevance; they are not the source of the figures above.

---

## Performance

### What is instrumented

The `/metrics` endpoint exposes Prometheus metrics, updated live by `brain/core/metrics.py`:

| Metric | Type | What it tracks |
|---|---|---|
| `brain_request_latency_seconds` | histogram | Request latency, bucketed at 0.05 / 0.1 / 0.25 / 0.5 / 1.0 / 2.5 / 5.0 / 10.0 s |
| `brain_request_count_total` | counter | Requests served |
| `brain_error_count_total` | counter | Failed requests |
| `brain_cache_hits_total` / `brain_cache_misses_total` | counter | Retrieval cache effectiveness |
| `brain_llm_tokens_total` | counter | Prompt + completion tokens |
| `brain_llm_cost_usd_total` | counter | Cumulative LLM spend |
| `brain_extraction_throughput_total` | counter | Facts extracted |

Because every LLM call passes through `llm.py`, token and cost accounting covers the whole system rather than whichever call sites remembered to record.

### Latency budget

The design target is **p95 < 300 ms for retrieval**, excluding LLM synthesis. The stages are separated because only the first group is under the system's control:

| Stage | Under our control | Notes |
|---|---|---|
| Redis cache hit | yes | Skips embedding and both DB queries entirely |
| Vector search (HNSW) | yes | Sub-linear; degrades to sequential scan if the index is missing |
| Full-text search (GIN) | yes | Scales logarithmically |
| RRF merge | yes | Pure Python over two short lists |
| Query embedding | no | Network call to the embedding provider |
| LLM synthesis | no | External API — **excluded from the retrieval budget** |

Synthesis dominates end-to-end latency and is bounded by the provider, which is why retrieval is measured separately and why the pipeline degrades to returning ranked facts without prose when the LLM is slow or unavailable.

### Measured stage latency

Median of 3 queries against remote Supabase (`ap-northeast-1`) from a development machine, warm connection:

| Stage | Median | Range |
|---|---|---|
| Query embedding (Gemini, network) | 1225 ms | 1063–1656 ms |
| Vector search (pgvector) | 229 ms | 160–932 ms |
| Full-text search (Postgres GIN) | 231 ms | 132–335 ms |

**The embedding round-trip is ~5× the cost of both database queries combined**, and it is the one stage that is a third-party network call. The in-process embedding cache exists for exactly this reason — a repeated query skips it entirely.

These figures are dominated by geographic round-trip to a remote database and will not resemble a co-located deployment. They are reported to show *relative* stage cost, not as a throughput claim.

### Measuring it

```bash
k6 run tests/load/k6_load_test.js                    # reports search_latency p(95)
locust -f tests/load/locust_load_test.py --headless -u 20 -r 5 --run-time 2m \
       --host http://localhost:7070
python scripts/explain_queries.py                    # fails if a hot query seq-scans facts
curl http://localhost:7070/metrics | grep brain_request_latency
```

`scripts/explain_queries.py` exits non-zero if any hot query falls back to a sequential scan on `facts`, which is the usual sign the HNSW index is missing or unused.

> **Note on numbers.** No measured throughput or latency figures are published here. Results depend on hardware, corpus size, network distance to the model provider, and cache warmth — a number from one environment would not predict another. `docs/performance.md` gives the per-stage budget and tuning guidance; the commands above produce figures for your own deployment.

---

## Testing and CI

**293 test functions across 17 files** (301 collected cases with parametrisation). The last full local run, excluding the Playwright and load suites: 279 passed, 7 skipped, 0 failed.

| Suite | Scope |
|---|---|
| Unit | Retrieval fusion, ACL logic, extraction, validation, auth |
| Integration | Full pipeline against a live Postgres — ingest → extract → retrieve → answer |
| **Isolation** | Tenant isolation, RLS enforcement, privilege escalation, SQL injection |
| E2E | Playwright journeys over a live server: signup, invite, chat privacy, cost caps |
| Load | Locust scenarios |
| **Benchmark gate** | `tests/test_regression.py` scores the retrieval benchmark against `evals/thresholds.yaml` (local database + embedding key); the LLM-judged parts run with `BENCH_LLM=1`. CI runs only its architecture guards |

CI runs seven jobs on every push: `lint` · `typecheck` · `unit-tests` · `integration-tests` · `isolation-suite` · `secret-scan` · `dependency-scan`, with Postgres and Redis as live services.

---

## Running locally

**Requirements:** Python 3.11+, Postgres 16 with pgvector, Redis (optional)

```bash
git clone <repo-url> && cd thread
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt && pip install -e .

cp .env.example .env        # then fill in DATABASE_URL and one LLM API key

python -c "from brain.core import db; c=db.connect(); db.migrate(c); c.close()"
uvicorn brain.wrappers.api:app --reload --port 7070
```

Open `http://localhost:7070`.

```bash
pytest tests/ -v                    # full suite (integration tests need DATABASE_URL)
pytest tests/test_isolation.py -v   # security isolation suite
python -m evals.bench               # retrieval, answer quality and supersession benchmark (local DB only)
```

---

## Project layout

```
brain/
  engine.py            Brain — the single public API surface
  config.py            All env loading and defaults
  auth.py              JWT verification, identity extraction, dev-bypass guard
  worker.py            Background ingestion queue (arq)

  core/
    db.py              Public DB surface (re-exports db_pg)
    db_pg.py           Postgres + pgvector: schema, migrations, RLS, queries
    retrieve.py        Hybrid search, RRF, ACL filtering, synthesis
    extract.py         Episode → structured facts
    ingest.py          Content-hash dedup + ingestion
    embed.py           Gemini embedding client (768-dim)
    llm.py             Single metered LLM choke point
    resilience.py      Circuit breakers, retry decorators
    metrics.py         Prometheus instrumentation
    connectors/        Slack (direct) + multi-app integration provider

  wrappers/
    api.py             FastAPI REST server
    mcp_server.py      MCP server (Claude Code, Cursor)
    slack_bot.py       Slack bot
    cli.py             Command-line interface
    hooks/             Claude Code lifecycle hooks
    webapp/            Web UI + landing page

evals/                 Quality benchmark: metrics, question set, thresholds
migrations/            Alembic schema versions
tests/                 Unit, integration, isolation, E2E, load
scripts/               Ops utilities: index rebuild, RLS check, benchmarks
monitoring/            Alert rules
```

---

## Engineering decisions

**Why hybrid retrieval instead of pure vector search?**
Embeddings are weak at exact-match tokens — error codes, service names, version numbers. Full-text handles those but misses paraphrase. RRF needs no score normalization between the two systems, only ranks, which makes fusing them robust.

**Why is filtering after fusion, not inside the SQL?**
Pushing ACL predicates into both retrieval queries would mean two places to get authorization right, and a ranking change could silently bypass one. A single post-fusion filter is the narrower path to audit.

**Why route every LLM call through one module?**
Token metering, cost caps, BYO-key routing, and circuit breaking are cross-cutting. At each call site they'd be inconsistently applied; at one choke point they're guaranteed.

**Why enforce isolation in Postgres rather than the application?**
Application checks are one bug away from a data leak. RLS means the database refuses the read even when the query is wrong — defence in depth, and the isolation suite proves it holds.

**Why content-hash deduplication?**
Connectors re-deliver the same message on re-sync and webhook replay. Hashing content makes ingestion idempotent, so the same Slack thread ingested twice produces one episode.

---

## Current limitations

Stated plainly, because knowing what isn't built matters as much as what is:

- **Key encryption uses a single environment-held master key**, not a managed KMS. There is no automated key rotation — rotating `BRAIN_KEY_ENCRYPTION_KEY` requires customers to re-enter their keys.
- **No SSO or SCIM.** Authentication is Clerk-based; there is no SAML or directory provisioning.
- **No data residency controls.** The deployment region is wherever it's hosted; there's no per-tenant region selection.
- **No SOC 2 attestation.** The controls above are real and tested, but no third-party audit has been performed.
- **Queries reach third-party model providers.** Answer synthesis calls an external LLM, so query text leaves the deployment.
- **A failed embedding call is stored as a zero vector.** The new OpenRouter fallback makes this rarer, but if both providers are down a fact is saved that no query can find until it is re-embedded (`scripts/reembed_gemini.py`). The benchmark aborts in that case; the application does not.
- **The supersession judge is an LLM.** It is tuned on a synthetic benchmark of 16 updates and has not been validated on real team data; see the limits listed under [Evaluation](#evaluation).
