# Boundaries, state, safety and operations — what is implemented and what is not

This document answers one question honestly: **for each operational concern, what does the code actually do today, what is it missing, and what is only an intention?** Every statement comes from reading the code, running it, or running the tests; where something was checked only by reading, it says so.

**Status labels**

| Label | Meaning |
|---|---|
| **Implemented** | Works, and a test or a run demonstrates it |
| **Implemented, with limits** | Works, but a stated limitation or weaker guarantee applies |
| **Defective** | Code exists but does not do what it claims (found by checking) |
| **Not active in production** | Correct code that the current Vercel deployment does not run or install |
| **Planned** | Not built. Listed so nobody mistakes an intention for a feature |

A pass over the code for this document found problems that earlier documents did not state. They are listed in section 10; the three privacy leaks among them were fixed in the same pass (section 5) and are covered by new tests.

---

## 1. Tool and MCP boundaries

### 1.1 The surfaces and what each can do

| Surface | Runs as | Reads | Writes | Identity comes from |
|---|---|---|---|---|
| **REST API** (64 endpoints, Vercel) | serverless function | search, history, facts, ledger, teams, members, usage | record, correct, teams, KPIs, connectors, members, invites, billing | a verified Clerk token, or a signed 4-hour guest token |
| **Web app** | browser, calls the REST API | same as the API | same as the API | the user's Clerk session |
| **MCP server** (`python -m brain.wrappers.mcp_server`) | a local process on the user's machine | `search_company_facts`, `get_decision_history`, `list_teams` | `record_fact`, `correct_fact`, `harvest_from_response` | **environment variables** read at process start (`BRAIN_USER`, `BRAIN_PRINCIPALS`, `BRAIN_WORKSPACE`, `BRAIN_TEAM`) |
| **Claude Code hooks** | local script fired by the editor | before each prompt: searches and injects facts | after each reply: extracts and stores facts | the same environment variables |
| **CLI** (`brain ...`) | local process | search, history, ledger, teams | record, correct, sync, backfill | the same environment variables |
| **Slack bot** | routes inside the API process | answers @mentions and DMs | none (Slack ingestion was removed) | the Slack user, linked to an account by email |

### 1.2 The MCP tools

All six tools take plain strings and return a JSON string. They never raise: a failure comes back as `{"error": "..."}` so the calling agent can read it. Inputs are checked for emptiness; `record_fact` is capped at 2,000 characters; extraction truncates any text to 6,000 characters before it reaches a model.

| Tool | Kind | What it does | Guards |
|---|---|---|---|
| `search_company_facts` | read | full retrieval and a cited answer | empty query rejected |
| `get_decision_history` | read | timeline including superseded facts | empty subject rejected |
| `list_teams` | read | the workspace's teams | none |
| `record_fact` | write | stores a human-authored fact, confidence 1.0 | non-empty, ≤ 2,000 chars; reconcile may report a duplicate or retire an older fact |
| `correct_fact` | write | retires a fact and records its replacement | fact must exist; caller must be in the fact's ACL |
| `harvest_from_response` | write | extracts facts from an assistant message, confidence ≥ 0.7, tagged `source_kind="agent"` | near-duplicate check via reconcile |

### 1.3 Where the trust boundary actually is — **Implemented, with limits**

The MCP server is **not a networked service with per-call authentication.** It is a local process that opens its own database connection from `DATABASE_URL`. Whoever can start it holds the database credentials and one fixed identity. That is a sound design for one developer's own machine, and the wrong one for a shared deployment. Specific consequences, all verified by reading the code:

- **A caller can choose any team.** `team_id` is a tool argument, and visibility only checks that a fact's team matches the requested one, so an agent can read another team's facts by naming it. The REST API takes the team from the token instead.
- **`cross_team=True` is not gated.** The REST API allows it only for admins; the MCP tools accept it from anyone.
- **`correct_fact` is effectively open.** Its permission check is ACL membership, and nearly every fact has ACL `everyone`, so any MCP caller can supersede nearly any fact in the workspace. It also checks the default team's brain, not the `team_id` the other tools accept.
- **There is no confirmation step before a write.** An agent that calls `record_fact` or `harvest_from_response` changes shared memory immediately; the only trace is `source_kind` and the ledger.
- **One connection for the life of the process** and no reconnect logic: if the database drops it, later calls fail until the MCP process is restarted.
- **No rate limit** on MCP calls (the REST API has one).

**Planned, not built:** per-call tokens or scoped keys for MCP, an admin check on `cross_team`, a team-membership check on `team_id`, a review queue for agent-written facts.

### 1.4 What protects the model from what the tools return — **Implemented**

Retrieved text is treated as data, not instructions, at three points: ingested content has eight instruction-like patterns replaced with `[filtered]` before it is stored; extraction and answer prompts wrap content in `<untrusted_content>` tags with an explicit rule never to obey it; and the finished answer is scanned for secret-shaped strings and for jailbreak-shaped questions before it is returned. The end-to-end benchmark planted two injection attempts in ingested messages and asked three hijack questions: 3 of 3 were resisted (single run).

### 1.5 The hooks never block the user — **Implemented**

`UserPromptSubmit` searches memory and injects facts only if at least one has confidence ≥ 0.4, and logs the injection to the ledger (`hook-inject`). `Stop` harvests the assistant's message (`hook-harvest`). Both exit successfully whatever happens; errors go to stderr only, so a failing hook is invisible to the user.

---

## 2. Context and state

### 2.1 Where state lives

| Scope | What | Lifetime | Status |
|---|---|---|---|
| **Request** | the resolved identity; a `Brain` with its database connection; the billing context (a `ContextVar`, so concurrent requests cannot overwrite each other) | one request | Implemented |
| **Process** | query-embedding cache (256 entries), user-to-account cache, rate-limit counters, guest daily cap, circuit-breaker states, Clerk key cache, integration-catalogue cache (1 hour) | until the instance is recycled; **on serverless each instance has its own, and all of it is lost when the instance freezes** | Implemented, with limits |
| **Durable (Postgres)** | episodes, facts and their embeddings, fact edges, the search ledger, accounts and memberships, conversations and messages, usage, sync watermarks, the shadow log | permanent | Implemented |
| **Redis** (optional) | query-result cache (300 s), webhook de-duplication, the `arq` job queue | — | **Not active in production** (no Redis is provisioned; every call degrades to a no-op) |

### 2.2 What the model is given for each question

For a question, the answer model receives: the top **5** facts (the top **8** when the question is about how something changed); for each, its "as of" date and a "superseded" label when it has been replaced; today's date; the asking team's mission and KPIs; and fixed rules (answer only from the facts, cite fact IDs, abstain when unsure, never reveal secrets). **Nothing from earlier turns is included.**

- **Multi-turn memory is not implemented.** Search is stateless. Conversations and messages are stored (strictly per user) so the interface can show history, but nothing from a previous turn is fed back into retrieval or the prompt. A follow-up such as "and who owns it?" does not know what "it" was.
- **Time is modelled** in two ways: facts carry `introduced_at` and `invalidated_at`, and a replaced fact is kept, not deleted. A fact's "as of" date is the source message's own date when known.
- **Planned, not built:** conversational memory, entity or user profiles, consolidation of old facts into higher-level ones.

### 2.3 Session state for clients

A user session is a Clerk token the browser refreshes before each call. A guest session is a signed token valid four hours, locked to the demo workspace; the guest question cap of 20 per session is enforced in the browser only, while a global cap of 100 per day is enforced on the server (in process memory, so per instance).

---

## 3. Evaluation

What is measured, and what is not. All figures are from a controlled benchmark on a fictional company; **none is a measurement on real production use.**

| Layer | What it measures | Where | Headline result |
|---|---|---|---|
| **Retrieval** | hit@5, recall, precision, MRR, nDCG; vector vs full-text vs hybrid | `evals/bench.py` Part A | hit@5 1.000 (44 facts, so a ceiling); full-text alone 0.244 |
| **Answer quality** | correctness, faithfulness (judge), citation validity and support, abstention | Part A | 135/135 correct; 143/143 citations real and supportive; 30/30 unanswerable refused; faithfulness 0.956 |
| **Supersession** | which new fact replaces which, against three methods | Part B | F1 0.85, against 0.12 (old method) and 0.48 (similarity alone) |
| **Pipeline state** | facts pushed through the real save path, then queried | Part C | 41/42 changing-fact chains correct, against 0/42 with the step off |
| **End to end** | raw messages through ingest, extraction, reconcile, retrieval, answering | `evals/e2e.py` | claims captured 38/39; chains 11/12; 0 wrongly retired; 26/28 answers correct (single run) |
| **Regression gate** | floors on retrieval (and, with `BENCH_LLM=1`, answers and supersession) | `tests/test_regression.py`, `evals/thresholds.yaml` | skips with a reason when embeddings are unavailable instead of scoring zeros |
| **Real data** | the same metrics on a team's own questions | `evals/real_eval.py`, shadow mode | **Tooling built; never run** |

**Not measured at all:** fact-extraction F1 on real text, MCP tool-selection quality, ingestion success rate over time, behaviour under concurrent load (the Locust and k6 scripts exist and were never run here), production latency, and any figure from real users.

---

## 4. Permission and approval

### 4.1 Who may do what — **Implemented**

| Role | Source | Capabilities |
|---|---|---|
| **guest** | signed demo token | read-only, locked to `ws_demo`; blocked by `require_not_guest` from every write and setting |
| **member** | Clerk `public_metadata.role` (default) | search, history, facts, own conversations, own connections; their own team's ledger |
| **admin / owner** | Clerk `public_metadata.role` | everything a member can, plus members and invitations, teams, KPIs, connectors backfill, billing, usage, BYO keys, dead-letter admin, account export, cross-team search |

Counted in `api.py`: **28** endpoints require an admin, **9** more reject guests, and **9** call sites apply a per-user rate limit (60 requests a minute on model-backed routes, 120 on writes, 20 on sign-up). A user cannot change their own membership; roles an admin may assign are `employee` and `admin`; seat limits and plan caps are enforced (HTTP 402).

**Limit worth knowing:** a user's effective role is whatever Clerk's `public_metadata` says. The `memberships.role` column is written at sign-up and invitation but is only displayed, not used for authorisation.

### 4.2 Consent and credentials — **Implemented**

Connectors use each user's own OAuth consent through the integration provider, one connection per user, never shared with teammates. The callback that completes OAuth now trusts only a signed, expiring state value created by the authenticated connect call. Invitations carry a 32-byte random token and expire after seven days; a Slack user is linked to an account only by matching email. Customer-supplied model keys are encrypted at rest.

### 4.3 Approval workflows — **Not implemented**

Nothing in the product asks a human to approve a change to knowledge. Extraction writes facts, the reconcile step retires older ones, and agents can write through MCP and hooks, all without review. What exists instead is containment:

- facts are **never deleted** by the pipeline, only marked superseded, so a wrong decision can be inspected and reversed by hand;
- every fact carries `source_kind` (`llm`, `human`, `agent`, `ai-tool`, `integration`) and an episode link;
- **shadow mode** (`BRAIN_RECONCILE_MODE=shadow`) records what reconcile would have done without doing it, so decisions can be audited before they take effect;
- `evals/real_eval.py` exports those decisions for human review and computes their precision.

**Planned, not built:** a review queue for supersession and agent-written facts, per-tool permissions, an approval step before an agent write.

---

## 5. Tenant isolation

### 5.1 The layers — **Implemented**, with the limits below

| Layer | How | Verified by |
|---|---|---|
| Identity | workspace and team come from the verified token, never from request bodies; guests are forced to `ws_demo` server-side | `test_auth.py`, `test_isolation.py` |
| Query scoping | every query carries `workspace_id`; the query cache key includes workspace, principals, team and flags | `test_isolation.py`, `test_ingest_retrieve.py` |
| Fact visibility | `team`, `shared`, or `private` (one person's connector data, returned to its owner alone, even for admins and cross-team searches) | `test_privacy.py`, `test_private_data_paths.py` |
| ACL | a caller's principals must intersect a fact's ACL, applied after ranking so no ranking path skips it | `test_retrieval.py` |
| Row-level security | enabled on episodes, facts, fact edges, ledger, teams, KPIs; forced, with per-user policies, on conversations and messages | `scripts/db/check_rls.py` (CI gate) |
| Connector ownership | a connection's data is stamped private and owned by the connecting user; the reconcile step never shows the judge a private fact on someone else's behalf | `test_reconcile.py` |
| Encryption | customer model keys are encrypted with a master key held only in the environment | `test_metering.py` |

Totals: 15 tests in the isolation suite (a hard CI gate), 15 in the privacy suite, 7 in the private-data-paths suite added below.

### 5.2 Three leaks found and fixed in this pass

Reading every endpoint that touches facts or episodes found three places where one person's private connector data (for example facts extracted from their mailbox) reached other people. All three are fixed, each with a test that **fails on the old code** (6 of the 7 new tests do):

1. **`GET /api/facts`** listed a team's facts without a visibility check, so a teammate saw another person's private facts; an admin using cross-team saw everyone's. It now lists a private fact only to its owner, and no longer sends the embedding vector to the browser.
2. **The search ledger** stored the text of every fact a search returned, including the searcher's private ones, and `GET /api/ledger` returned that text to teammates and admins. Private fact text is no longer written, and the API no longer returns the context column.
3. **`GET /api/account/export`** dumped every episode body and fact in the workspace to an admin, private mail included. It now excludes private episodes, facts and any edge touching one.

**Action needed on a deployed database:** ledger rows written before this fix may still contain private text. Run `UPDATE ledger SET context_sent = NULL;` once (the column is not used by the product).

### 5.3 What the isolation does not give you — **limits**

- Row-level security on the knowledge tables is **enabled but not forced**, and the application connects as the table owner, which bypasses non-forced policies. The real control is the `workspace_id` predicate in each query, with RLS as a second layer. Connecting as a non-owner role would make it binding; this has not been done.
- The MCP server and CLI bypass the REST API's authentication entirely (section 1.3).
- All tenants share one database; there are no per-tenant encryption keys and no data residency control.
- Ledger rows show every teammate's query text to admins (and a team's queries to that team). That is an audit feature, not a leak, but it is visible.

---

## 6. Idempotency

| Operation | Mechanism | Safe to repeat? |
|---|---|---|
| Ingest a message | SHA-256 of the scrubbed body, unique on `(workspace, hash)`, `ON CONFLICT DO NOTHING` | **Yes** — re-syncs and replays add nothing |
| An edited item (same external id, new content) | detected; the new episode records which it supersedes | **Yes** |
| Scheduled sync | per-source watermark advances only on success; a failed pass refetches its window; two syncs never overlap (GitHub `concurrency` group) | **Yes** |
| Extraction of one message | the episode is marked extracted only after every fact is saved | **Not strictly:** a crash after saving some facts re-extracts the message on retry. Reconcile's duplicate check usually absorbs the repeats, but that is a model judgement, not a guarantee |
| Recording a fact | new random id each call | **No** — the same sentence twice can create two facts unless reconcile flags a duplicate |
| `correct_fact` | invalidates, then records a replacement | **No** — repeating it creates another replacement |
| Webhook delivery | signature check, delivery-id de-duplication in Redis, queue job id from the delivery id | Implemented, **not active in production** (needs Redis; with Redis down it fails open and relies on content-hash de-duplication) |
| Sign-up / SSO provisioning / default subscription | checks for an existing membership; `ON CONFLICT DO NOTHING` | **Yes** |
| Schema creation | every statement is `IF NOT EXISTS` or guarded | **Yes** |
| Slack event handling | answered in a background thread | **No** — Slack retries a slow event and nothing de-duplicates by event id |

---

## 7. Retries and recovery

| Failure | What happens | Status |
|---|---|---|
| A model call fails | up to 3 attempts with exponential backoff and jitter; then the fallback model **if the route names a different one** (calls that name a model explicitly fall back to the same model, which is how the pipeline runs); a circuit breaker (5 failures, 30 s) then fails fast | Implemented, with limits |
| The model is down at answer time | the response returns the ranked facts without prose and says so | Implemented |
| The model is down at extraction | the message stays unextracted and is retried on the next pass; the benchmark runner gives up after two passes with no progress | Implemented |
| A model returns malformed output | one bad fact is dropped, the rest kept; a null field defaults to empty | Implemented (fixed in this work) |
| Embeddings fail | breaker after 3 failures; same model through a second provider; if both fail, extraction refuses to save and retries later | Implemented. Manually recorded facts can still be saved without a usable vector |
| The database connection is dead | validated on checkout, discarded and replaced (up to 3 tries) | Implemented |
| A connector sync fails | failure recorded, watermark not advanced | Implemented. **The backoff window it records is not honoured by the scheduled script** (only the worker reads it) — with a 15-minute schedule this means a failing source is retried every 15 minutes |
| Redis is down | every cache and queue call is a no-op | Implemented |
| A job exhausts its retries | the worker intends to write it to a dead-letter table with admin replay | **Defective:** the hook it uses, `on_job_abort`, is not an `arq` hook in the installed version (0.28), so the dead-letter table is never written. The worker is also not deployed |
| A sync has fallen behind | the worker's reconcile job computes an overlap window to re-poll | **Defective:** the window is computed and logged but never passed to the job, so nothing is re-polled. Not deployed |
| Database loss | Supabase backups (plan-dependent) and documented `pg_dump` / restore steps | Documented, not automated or tested here |

Breaker and cache state are per process, so on serverless every instance learns about an outage separately.

---

## 8. Logs and observability

| Capability | Implemented | Active on the current Vercel deployment |
|---|---|---|
| Structured JSON logs with a per-request trace id, echoed in `X-Trace-Id` | Yes | **Yes** (to Vercel's log stream; retention is the plan's) |
| Search ledger (who asked, what, which fact ids) | Yes | **Yes** (private fact text no longer stored) |
| Per-call token and cost records (`usage_events`, never the text) | Yes | **Yes** — cost is approximate for models missing from the price table, which are billed at a default rate |
| `/healthz`, `/readyz` (database, Redis, model key, config, vector index) | Yes | **Yes**, but no external monitor is configured in the repository |
| Prometheus metrics at `/metrics` | Yes | **No scraper** exists; counters live in process memory and reset with each instance |
| Alert rules (`monitoring/alerts.yaml`: error rate, p95 latency, queue backlog, cost, connector threshold, auth failures) | Definitions only | **No**: nothing evaluates them, and no Prometheus or Alertmanager is wired |
| Sentry error tracking with a scrubber | Code exists | **No**: initialised only inside `main()`, which Vercel never runs, and `sentry-sdk` is not in the deployed requirements |
| Langfuse tracing of model calls | Code exists | **No**, for three independent reasons: `langfuse` is not in the deployed requirements; the code calls `Langfuse.trace()`, which does not exist in the current SDK (4.x), so the call fails and is swallowed; and the client is never flushed, which a short-lived function needs |
| Boot configuration check | Yes | **No**: it runs only in `main()`; the import-time auth checks do run |

What is logged and what is not: HTTP logs carry method, path, status and duration, never bodies; model calls log the task and model name only; the ledger stores the question text. Sentry's scrubber removes query, answer, email and tokens. **Langfuse would receive full prompts, including private facts, if it were switched on**, so it would need masking, or self-hosting, first.

---

## 9. Deployment

| Piece | How it is done | Notes |
|---|---|---|
| **Compute** | Vercel Python function, region `hnd1`, entry `api/index.py`; every route goes to it | the deployed dependency list is `api/requirements.txt`, which is shorter than the root `requirements.txt` (no Redis, Sentry, Langfuse, `cryptography`) |
| **Database** | Supabase Postgres + pgvector, transaction pooler (port 6543), same region | the HNSW index is built by hand (`scripts/db/rebuild_index.py`); `/readyz` reports it missing |
| **Schema** | created idempotently by `migrate()` on the first request of each cold instance; Alembic holds six revisions that the deployment does not run | two sources of schema truth |
| **Sync** | GitHub Actions cron every 15 minutes, `Production` environment, fails fast on missing secrets | the real ingestion path in production |
| **CI** | on every push and PR: lint, type-check (non-blocking), unit, integration with Postgres and Redis, RLS check, isolation suite as a hard gate, secret scan, dependency audit; E2E (Playwright) in a second workflow | the retrieval gate and benchmark are not in CI |
| **Release** | a push to `main` redeploys on Vercel | **nothing ties CI to the deploy**: Vercel builds on push whether or not CI passed |
| **Secrets** | Vercel environment variables, GitHub Environment secrets; gitleaks in CI | a pre-push check against the literal values in `.env` is how this work was verified |
| **Rollback** | Vercel can promote a previous deployment (a platform feature, not scripted here); database changes are additive, so there is no down-migration in use | |
| **Local** | Docker (`pgvector/pgvector:pg16`), `uvicorn`; benchmarks refuse to run against a non-local database | there is no Dockerfile, compose file, staging environment or infrastructure-as-code |

---

## 10. Summary

### Implemented and demonstrated
Ingestion with de-duplication and injection scrubbing; LLM extraction with per-fact tolerance; semantic supersession with a judge, duplicate detection, shadow and off modes; hybrid retrieval with ACL, team and private filtering; answers with citations, dates, superseded labels, abstention and an output guardrail; decision history; tenant scoping with a CI RLS gate; the guest demo; invitations, roles, seat and plan limits; the scheduled sync; per-call usage records; per-stage-validated benchmarks and a regression gate.

### Implemented, with limits
MCP and CLI trust (single local identity, ungated team override); RLS (enabled, not forced); retries (explicit models have no real fallback); idempotency of extraction and manual writes; rate limits and breakers (per instance); evaluation (controlled data, mostly single runs).

### Found defective in this pass
1. The dead-letter hook is not an `arq` hook (the table is never written).
2. The worker's gap-healing overlap is computed and never applied.
3. The scheduled sync records a backoff window and then ignores it.
4. Langfuse's call does not exist in the SDK version that installs, and is never flushed.
5. Sentry is never initialised or installed on the deployed path.
6. Three private-data leaks (facts list, ledger, export) — **fixed, tested, uncommitted**.
7. Slack events are not de-duplicated.
8. The `/facts` slash command read fields that do not exist (fixed).

### Planned or absent
Per-call MCP authentication and scopes; a review or approval step for agent-written and superseded facts; conversational memory; a binding row-level security role; real-data validation; chunking of long documents; alert delivery; a staging environment and a CI-gated deploy; fact-extraction F1 on real text; load testing.
