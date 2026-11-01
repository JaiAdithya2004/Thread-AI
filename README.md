# Thread — AI Memory for Software Teams

Thread turns scattered team knowledge across workplace tools into **searchable, source-linked facts**. It helps teams answer questions like *“Why did we choose Postgres over DynamoDB?”* without digging through old emails, issues, documents, or chat threads.

Unlike a basic RAG chatbot, Thread extracts structured facts, tracks how they change over time, preserves decision history, and generates answers grounded in retrieved source facts.

> **Core idea:** Connect workplace knowledge, reconcile changing facts, and retrieve answers with provenance instead of treating every message as an isolated document chunk.


## Key capabilities

- **Connected knowledge ingestion:** Uses Composio-managed integrations for sources such as Gmail, GitHub, Google Calendar, Notion, and Slack.
- **Structured fact extraction:** Converts messages and documents into facts with source references, confidence information, team scope, and access controls.
- **Hybrid retrieval:** Combines semantic vector search with PostgreSQL full-text search and merges rankings using Reciprocal Rank Fusion (RRF).
- **Fact reconciliation:** Uses embedding-based candidate retrieval and an LLM judge to classify potential duplicates, superseded facts, contradictions, and unrelated information.
- **Versioned organizational memory:** Marks superseded facts as stale rather than deleting them, preserving historical decisions and changes.
- **Grounded answers:** Generates concise responses with citations to retrieved fact records and abstains when evidence is insufficient.
- **Multiple interfaces:** Includes a web UI and REST API, plus MCP, CLI, and Slack-facing interfaces in the codebase.

## How it works

```mermaid
flowchart LR
    A[Workplace sources<br/>Gmail · GitHub · Calendar · Notion · Slack] --> B[Connector sync]
    B --> C[Normalize and deduplicate]
    C --> D[LLM fact extraction]
    D --> E[Fact reconciliation]
    E --> F[(PostgreSQL + pgvector)]
    F --> G[Hybrid retrieval<br/>Vector + full-text]
    G --> H[Access and team filters]
    H --> I[LLM answer synthesis]
    I --> J[Answer with source citations]
    E --> K[Version and conflict history]
```

1. **Ingest:** Connectors retrieve source content incrementally. Content hashes make repeated syncs idempotent.
2. **Extract:** An LLM turns relevant messages and documents into structured facts with source provenance and confidence information.
3. **Reconcile:** Existing candidate facts are compared with new facts to identify duplicates, updates, contradictions, or unrelated information. Superseded records remain available for historical queries.
4. **Retrieve:** Semantic search and PostgreSQL full-text search run together. RRF combines their rankings before access-control filtering.
5. **Answer and audit:** The system produces an evidence-grounded response with citations and records the query and returned fact references in an append-only ledger.

## Documentation

| Document | What it covers |
|---|---|
| [docs/PRODUCT_OVERVIEW.md](docs/PRODUCT_OVERVIEW.md) | What Thread is, the problem it solves, how its RAG design works, and where LangChain and Langfuse fit |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data model, request flows, deployment |
| [docs/SYSTEM_BOUNDARIES.md](docs/SYSTEM_BOUNDARIES.md) | Implemented vs. planned: MCP/tool boundaries, state, permissions, tenant isolation, idempotency, recovery, logging, deployment |
| [docs/performance.md](docs/performance.md) | Latency and cost notes |

## Evaluation at a glance

Controlled benchmark on a fictional 44-fact company (not production data; see [Evaluation](#evaluation) for method and caveats):

| Measure | Result |
|---|---|
| Retrieval hit@5 (hybrid) | 1.000 (a ceiling at this corpus size); full-text alone 0.244 |
| Answer correctness | 135 / 135 |
| Citations real and supportive | 143 / 143 |
| Unanswerable questions refused | 30 / 30 |
| Faithfulness (LLM judge) | 0.956 |
| Supersession F1 (embeddings + judge) | 0.85, vs 0.12 for the previous method and 0.48 for similarity alone |
| Changing-fact chains answered with the current value | 41 / 42, vs 0 / 42 with supersession off |
| End to end from raw messages (single run) | claims captured 38/39; chains 11/12; 0 wrongly retired; answers 26/28; prompt-injection attempts resisted 3/3 |

Not measured: extraction F1 on real text, MCP tool selection, load, production latency, or any real-user data. A real-data protocol exists (`evals/real_eval.py`) and has not been run.

## Architecture and technology

| Layer | Implementation |
|---|---|
| API and application | Python, FastAPI |
| Database | PostgreSQL 16, Supabase, pgvector, PostgreSQL full-text search, Alembic migrations |
| Retrieval | Vector similarity, full-text search, Reciprocal Rank Fusion, access-control filtering |
| LLM orchestration | LiteLLM, Gemini models, OpenRouter fallback |
| Embeddings | Gemini `gemini-embedding-001` (768 dimensions), with configured fallback |
| Connectors | Composio-managed OAuth integrations |
| Authentication | Clerk-backed authentication with server-verified sessions |
| Hosted application | Vercel serverless deployment and Supabase PostgreSQL |
| Scheduled ingestion | GitHub Actions runs incremental sync every 15 minutes in the current hosted configuration |
| Observability and resilience | Active in the hosted deployment: structured JSON logs with trace ids, the search ledger, per-call token/cost records, health/readiness endpoints, retries, circuit breakers, graceful degradation. Implemented but **not active** in the hosted deployment: Prometheus scraping and alert rules, Sentry, Langfuse (see [docs/SYSTEM_BOUNDARIES.md](docs/SYSTEM_BOUNDARIES.md)) |
| Development | Docker-supported local development; PostgreSQL with pgvector; Redis is optional for worker/cache paths |

The hosted sync uses a per-workspace, per-source checkpoint and advances it only after successful processing. Failed syncs record an error and retry with backoff instead of silently skipping the failed window.

The optional long-running worker and webhook path requires Redis and a persistent worker host. **It is not the current hosted ingestion path**; the hosted deployment uses scheduled one-shot syncs.

## Security and data isolation

- **Database-enforced tenant isolation:** PostgreSQL row-level security (RLS) protects tenant-scoped records alongside application-level authorization.
- **Fact-level access controls:** Facts carry team and visibility metadata; retrieval filters results according to the authenticated caller's permissions.
- **Server-derived identity:** Authorization uses the verified session rather than trusting a user ID supplied in a request body.
- **Secret handling:** Platform keys are loaded from environment variables. Customer-supplied LLM keys are encrypted at rest and are not returned by API responses.
- **Auditability:** Search requests record the query and the fact references returned, supporting investigation and review.
- **Safe failure behavior:** If answer synthesis is unavailable, the system can return ranked evidence rather than fabricate an answer; low-confidence queries can abstain.

## Evaluation

Quality is measured against explicit ground truth rather than asserted. There are four layers, from cheapest to most realistic:

| Layer | Question it answers | Tool |
|---|---|---|
| 1. Controlled benchmark | Does retrieval find the right fact, are answers grounded, and does the system know which fact is current? | `evals/bench.py` |
| 2. End-to-end benchmark | Does the whole pipeline work from raw messages: ingest, extract, reconcile, retrieve, answer? | `evals/e2e.py synthetic` |
| 3. Real-mailbox check | Does it hold up on real, messy email through the Composio Gmail connector? | `evals/e2e.py real` |
| 4. Regression gate | Does a code change make any of the above worse? | `tests/test_regression.py` |

Scope note: layers 1 and 2 use a fictional company so results are reproducible and contain no personal data. They show the system behaves correctly under controlled conditions; they are not accuracy figures for a live, production-scale workload.

### Test suite

- **365 automated tests pass** (10 skipped when optional services are unavailable), covering ingestion, extraction, reconciliation, retrieval, authentication, tenant isolation, privacy, resilience and the API.
- A tenant-isolation suite and a row-level-security check are hard gates in CI. Secret scanning and a dependency audit also run on every push.
- Browser (Playwright) and load (Locust, k6) suites exist in the repository and were not run for the figures below.

### 1. Controlled benchmark

`evals/bench.py` runs a fixed benchmark on a **synthetic company ("Meridian Labs")**.

| | |
|---|---|
| **Knowledge base** | 44 facts |
| **Answerable questions** | 45 (32 direct, 13 deliberately paraphrased with little word overlap), each with gold fact IDs |
| **Unanswerable questions** | 10 (the system should abstain) |
| **Changing facts** | 14 chains, 30 versions, 16 updates (a price rises twice, an owner changes, a vendor is replaced) |
| **Hard negatives** | 14 decoys that resemble an existing fact but must replace nothing, 8 paraphrases that should be detected as duplicates, 2 future-dated changes |

Ground truth is written by reading `evals/corpus.yaml`, never derived from what retrieval returned.

```bash
python -m evals.bench                          # all three parts; needs a LOCAL Postgres and LLM/embedding keys
python -m evals.bench --parts A --no-answers   # retrieval only: no chat model
python -m evals.aggregate                      # combine runs into the tables below
```

The benchmark recreates its own `ws_bench_*` workspaces, so it refuses to run against a non-local `DATABASE_URL`, and it aborts instead of scoring zero vectors if embeddings fail.

The LLM-judged parts are not deterministic, so the results are from **3 full runs** (2026-10-21): mean shown, counts pooled. Raw output is in `evals/results/`.

**Retrieval (k=5)**

| Ranker | Hit@5 | Recall@5 | Precision@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|
| vector | **1.000** (135/135) | 1.000 | 0.209 | 0.982 | 0.986 |
| full-text | **0.244** (33/135) | 0.244 | 0.049 | 0.244 | 0.244 |
| hybrid (RRF) | **1.000** (135/135) | 1.000 | 0.209 | 0.982 | 0.986 |
| production path | **1.000** (135/135) | 1.000 | 0.209 | 1.000 | 1.000 |

**Answer quality**

| Metric | Result |
|---|---|
| Answer correct (answerable questions) | **1.000** (135/135) |
| Faithfulness (LLM judge) | **0.956** (129/135) |
| Citation validity (cited id was retrieved) | **1.000** (143/143) |
| Citation support (LLM judge) | **1.000** (143/143) |
| Abstention on unanswerable questions | **1.000** (30/30) |
| Answer latency p50 / p95 | 3.97 s / 13.82 s |
| Retrieval latency p50 / p95 | 2.34 s / 3.64 s |

**Supersession decisions** (does a new fact correctly replace an old one?)

| Metric | Exact-subject (previous) | Embedding similarity only | Embedding + LLM judge (current) |
|---|---|---|---|
| Precision | 1.000 | 0.385 | **0.855** |
| Recall | 0.062 (3/48) | 0.625 (30/48) | **0.854** (41/48) |
| **F1** | 0.118 | 0.476 | **0.852** |
| Updates lost as "duplicates" | 0/48 | 9/48 | **0/48** |
| Paraphrases recognised as duplicates | 2/24 | 11/24 | **24/24** |
| Decoys wrongly acted on | 0/42 | 30/42 | **1/42** |
| Future-dated changes wrongly superseded | 0/6 | 6/6 | 6/6 |
| True predecessor among candidates | n/a | n/a | 47/48 (0.979) |

**End to end: changing facts through the real save pipeline**

| Metric | Reconciliation off | Reconciliation on (current) |
|---|---|---|
| Versions stored | 85/90 | **90/90** |
| Version status correct (valid vs stale) | 37/90 (0.411) | **89/90 (0.989)** |
| Chains fully correct | 0/42 | **41/42 (0.976)** |
| "Current X?" top result is the latest | 7/42 | **41/42** |
| "Current X?" answer states the latest value only | 0/42 | **41/42** |
| History returns every version | 85/90 | **90/90** |
| History marks stale/valid correctly | 37/90 | **89/90** |
| Unrelated facts wrongly invalidated | 0 | 0 |

**Models:** extraction and supersession judge `gemini-2.5-flash-lite`; answers `gemini-3.8-flash`; answer judge `gpt-4o-mini` (a different model family from the answerer, to avoid self-grading); embeddings `gemini-embedding-001`, 768-dim.

### 2. End-to-end benchmark (raw messages in, answers out)

`evals/e2e.py synthetic` feeds 36 raw messages through the **real** pipeline (noise filter, duplicate detection, injection scrubbing, LLM extraction, reconciliation, retrieval, answering) and scores each stage separately. The corpus includes noise, exact duplicates, edited messages, two planted prompt-injection attempts, and decoys. One run on 2026-10-22; extraction and synthesis `gemini-2.5-flash-lite`, judge `gpt-4o-mini`. Intervals are 95% Wilson.

| Stage | Metric | Result |
|---|---|---|
| Ingestion | Noise messages dropped | 1/1 |
| | Exact duplicates dropped | 1/1 |
| | Real messages kept | 32/32 |
| | Injection phrases scrubbed | 2/2 |
| Extraction | Claim recall (facts that should be found) | **38/39 (0.974)**, CI 0.87-0.99 |
| | Groundedness (every fact supported by its source) | **62/62** |
| | False facts created from noise | 0 |
| | Facts per kept message | 1.82 |
| | Claims stored twice | 3 |
| State | Claim in correct valid/stale state | 37/38 (0.974) |
| | Update pairs correctly linked | 11/13 (0.846) |
| | Chains fully correct | **11/12 (0.917)** |
| | Facts wrongly retired | **0** |
| | Missed retirements | 1 |
| | Decoys left alone | 2/2 |
| Retrieval | Hit@5 given the fact was extracted | 27/27 |
| | MRR / nDCG@5 | 0.915 / 0.926 |
| | "Current X?" top result is the latest | 9/11 (0.818) |
| Answers | End-to-end correct, all questions | **26/28 (0.929)** |
| | Direct / history / "current" questions | 13/13, 3/3, 10/12 |
| | Citation validity / support | 41/41, 39/41 |
| | Faithfulness | 24/28 (0.857) |
| | Abstention on out-of-scope | 5/5 |
| | **Prompt-injection attempts resisted** | **3/3** |
| Operations | Extraction time | 6.6 s per kept message |
| | Search latency p50 / p95 | 2.1 s / 8.2 s |
| | Provider cost for the extraction and retrieval run | $0.0225 |

The answer stage was run separately on the same corpus after the first answer pass was interrupted by an exhausted provider balance; the interrupted attempt is kept under `invalid_*` folders and excluded from every figure.

### 3. Real-mailbox check (Gmail via Composio)

The same pipeline was run on a real mailbox through the Gmail connector (2026-10-22). Only aggregates are published; the raw email text is kept in a git-ignored folder.

| Stage | Result |
|---|---|
| Ingestion | 30 emails fetched, 21 stored, 9 dropped as noise or duplicates |
| Extraction | 15 facts from 21 emails (most emails contain no durable decision); **15/15 stored as private to the owner**; **15/15 grounded in their source** |
| Retrieval | 15 questions: hit@5 15/15, MRR 1.00 |
| Answers | Faithfulness 15/15; citation validity 23/23; citation support 23/23; abstention on out-of-scope 8/8 |
| Reconciliation | No supersession candidates in this mailbox (0 superseded, 0 contradictions) |
| Health | All five stages valid; 0 blank answers; 0 unextracted emails |
| Cost / latency | $0.0094 provider cost; extraction 63.6 s; search p50 3.2 s |

This validates the connector path, privacy default and grounding on real text. It is a single mailbox and a small question set, and it contains no changing facts, so it says nothing about supersession on real data.

### What the numbers show

**Supersession is the headline.** Deciding "this new fact replaces that one" with embeddings to find candidates and an LLM to classify them reaches F1 0.85, against 0.12 for the previous exact-subject method and 0.48 for similarity alone. Through the real save pipeline, with reconciliation off no chain ended in the correct final state (0 of 42) and "what is the current X?" ranked the latest value first for only 17%; with it on, 41 of 42 chains are correct and no unrelated fact was invalidated. Two reasons the alternatives fail:

- *Embedding similarity alone cannot tell an update from a different fact.* Decoys scored 0.69-0.89 similar to the fact they resemble, overlapping the 0.73-0.98 of true updates, so a threshold either misses updates or replaces unrelated facts.
- *The previous method almost never fires.* It needs the extraction model to recall a fact it was never shown and match subjects exactly.

**Retrieval is solved at this scale, which says little about larger corpora.** Hit@5 is 1.000, but the corpus has 44 facts and K=5 returns 11% of it, so a ceiling is expected. Hybrid and vector-only tie; full-text alone finds the right fact for only 24% of questions because Postgres `plainto_tsquery` ANDs its terms, so semantic search carries the system.

**Answers are grounded.** Every answer contained the expected content (135/135), every citation was real and supportive (143/143), all 30 unanswerable questions were refused, and the end-to-end run resisted all planted injection attempts. Faithfulness of 0.956 reflects answers that appended a caveat sentence ("this is not specified") that the judge treated as unsupported.

**Defects found by measuring.** The end-to-end benchmark exposed four pipeline defects that unit tests missed: a null field in one extracted fact discarded all of that message's facts; facts from the same message could supersede each other; "how did X change" queries did not reach stale facts; embedding outages could silently save unusable vectors. All four were fixed and are covered by tests.

### Known weaknesses

- **Future-dated changes are mishandled.** "The Team plan *will* cost $89 starting January 1" invalidates today's $79 fact (6/6 in both LLM-based methods). Fixing it needs "effective from" semantics.
- **Seven of 48 updates were missed**, mostly changes of provider or location rather than of a number (AWS to Google Cloud, React Native to Flutter, Austin to Denver).
- **A decoy was superseded in one of three runs** (a PostgreSQL 16 analytics warehouse replacing the PostgreSQL 15 primary database).
- **End-to-end "current value" answers:** 10/12 correct, and only 6/12 stated the latest value without also repeating the older one.
- **Latency:** median answer about 4 s, p95 about 14 s, measured from a development machine over the network; it says nothing about a co-located deployment.

### Limits of this evaluation

- The corpus and questions were written by the person who built the system. The paraphrased questions and decoys are meant to counter that; an independent set would be stronger.
- The candidate floor (`RELATED_THRESHOLD` = 0.70) was chosen after looking at this benchmark's pair similarities and has not been validated on held-out data.
- 3 runs for the controlled benchmark and 1 for the end-to-end benchmark, on one machine; sample sizes are small (16 updates, 14 chains, 28 end-to-end questions), so intervals are wide.
- Embeddings occasionally came from a fallback provider (same model) when the primary was rate-limited; a full run on the fallback route gave candidate recall 16/16, supersession precision 0.86 and recall 0.75.
- **Not measured:** supersession on real data, extraction F1 on large real corpora, MCP tool selection, behaviour under concurrent load, production latency, or any real-user data. A protocol for scoring a team's own questions and reviewing live supersession decisions exists (`evals/real_eval.py`, `BRAIN_RECONCILE_MODE=shadow`) and has not yet been run.

### 4. Regression gate

`tests/test_regression.py` runs the retrieval benchmark against the floors in `evals/thresholds.yaml` and the recorded `evals/baseline.json`. It needs a local Postgres and an embedding key, and skips with a stated reason, rather than scoring zeros, when embeddings are unavailable. The LLM-judged parts run with `BENCH_LLM=1`. After an intentional change, refresh the baseline with `python -m evals.aggregate --write-baseline`.

| Metric | Hard floor | Tolerance vs baseline |
|---|---|---|
| Retrieval hit@5, recall@5 | 0.90 | 0.05 |
| Retrieval MRR, nDCG@5 | 0.85 | 0.05 |
| Answer correct / faithfulness / citation support | 0.90 / 0.85 / 0.90 | 0.08 |
| Supersession recall / precision | 0.80 / 0.70 | 0.10 |
| Decoys wrongly acted on (ceiling) | 0.20 | 0.10 |
| Chains fully correct; current-answer correctness | 0.70; 0.75 | 0.15 |

### Reproducing

```bash
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
python -m evals.bench && python -m evals.aggregate     # layer 1
python -m evals.e2e synthetic                          # layer 2
pytest -q                                              # test suite and regression gate
```
