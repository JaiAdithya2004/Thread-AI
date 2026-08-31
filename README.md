# Thread

**An AI memory layer for software teams.** Thread connects to the tools a team already uses, extracts structured facts from their conversations and documents, and answers questions with citations back to the source — through a web app, an MCP server, a Slack bot, or a REST API.

The problem it solves: the answer to "why did we choose Postgres over DynamoDB?" exists, but it's in a Slack thread from eight months ago that nobody can find. Thread makes that institutional knowledge queryable.

![Thread](Threadwebsite.png)

---

## Contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Retrieval pipeline](#retrieval-pipeline)
- [Security model](#security-model)
- [Evaluation](#evaluation)
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
3. **Retrieve** — a query runs through hybrid search, gets access-filtered, and is synthesized into an answer that cites the exact fact IDs used.
4. **Log** — every search writes to an append-only ledger: who asked, what they asked, and which facts came back.

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

**Stack:** FastAPI · Postgres 16 + pgvector · Redis · LiteLLM · Gemini embeddings (768-dim) · Alembic · arq

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
| **7. Rerank** | Cross-encoder rerank of the top 30, truncated to top-k. |
| **8. Synthesize** | LLM writes a prose answer citing fact IDs. Below a confidence threshold it abstains rather than guessing. |
| **9. Log** | Append-only ledger write. |

**Graceful degradation is deliberate.** If the LLM is unavailable, step 8 returns the ranked facts without prose rather than erroring — a degraded answer beats no answer. If Redis is down, every cache call is a no-op.

---

## Security model

Multi-tenancy is enforced in the database, not only in application code — a bug in a handler shouldn't be able to leak another tenant's data.

| Control | Implementation |
|---|---|
| **Tenant isolation** | Postgres row-level security on every tenant table (`episodes`, `facts`, `fact_edges`, `ledger`, `teams`), keyed on a session variable. |
| **Private chat threads** | `conversations` and `messages` use **FORCE** row-level security and drop to a non-`BYPASSRLS` role, so not even the table owner can read another user's threads. Admins cannot read employee chats. |
| **Identity** | Always derived server-side from the authenticated session. Never read from a request body, query string, or header. |
| **Fact-level access** | Each fact carries an ACL and a team; visibility is `team` or `shared`. |
| **Secrets** | Loaded from environment only — never stored in the database, logged, or returned in a response. |
| **Platform metrics** | Gated behind a `BRAIN_PLATFORM_OWNER` env flag, not a customer role, so no customer admin can reach cross-tenant figures. |
| **Dev auth bypass** | Refuses to start if enabled while `BRAIN_ENV=production`. |
| **Error responses** | 5xx details are logged server-side and returned opaque, so driver text and schema names never reach a client. |

A dedicated **isolation suite** (`tests/test_isolation.py`) treats these as testable claims — cross-workspace reads, admin-reading-employee-chat, invite token reuse, expired invites, and SQL injection attempts each have a test that must fail loudly if the control regresses.

---

## Evaluation

Retrieval quality is measured, not asserted. `evals/run.py` scores a fixed question set (`evals/questions.yaml`, 28 questions including deliberately unanswerable ones) and compares against a stored baseline.

**Retrieval metrics:** `hit@k` · `precision@k` · `recall@k` · `MRR` · `nDCG@k`

**Answer-quality metrics:**

| Metric | What it catches |
|---|---|
| `faithfulness_rate` | LLM-as-judge: is the answer actually supported by the retrieved facts? Catches hallucination. |
| `citation_precision` | Parses `[fact_id]` references out of the answer and verifies each one supports the sentence it's attached to. Catches *plausible but wrong* citations. |
| `abstention_accuracy` | On deliberately unanswerable questions, does it correctly say "I don't know"? Catches confident nonsense. |
| `answer_correct_rate` | Does the answer contain the expected substring? |

**Latency:** `mean_latency_s`, `p95_latency_s`.

Each metric has a **tolerance** (how far it may drop below baseline before it's flagged a regression) and a **hard floor** that catches catastrophic collapse regardless of baseline — see `evals/thresholds.yaml`. Abstention accuracy is measured because a memory system that confidently invents answers is worse than one that admits ignorance.

---

## Testing and CI

**152 test functions across 18 files.**

| Suite | Scope |
|---|---|
| Unit | Retrieval fusion, ACL logic, extraction, validation, auth |
| Integration | Full pipeline against a live Postgres — ingest → extract → retrieve → answer |
| **Isolation** | Tenant isolation, RLS enforcement, privilege escalation, SQL injection |
| E2E | Playwright journeys over a live server: signup, invite, chat privacy, cost caps |
| Load | Locust scenarios |

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
python evals/run.py                 # retrieval + answer quality benchmark
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

- **BYO API keys are stored in plaintext.** Envelope encryption with a KMS is designed but not implemented — the column exists, the wrapping does not.
- **No SSO or SCIM.** Authentication is Clerk-based; there is no SAML or directory provisioning.
- **No data residency controls.** The deployment region is wherever it's hosted; there's no per-tenant region selection.
- **No SOC 2 attestation.** The controls above are real and tested, but no third-party audit has been performed.
- **Queries reach third-party model providers.** Answer synthesis calls an external LLM, so query text leaves the deployment.
