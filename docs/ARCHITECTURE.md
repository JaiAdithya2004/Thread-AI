# Thread — architecture, data flow and measured results

This document explains what the system does, how data moves through it, what it runs on, and how well it works, in that order. It is written from a read of the code and from the benchmark runs of 2026-10-21; where something was not verified against the live deployment, it says so.

---

## 0. The short version

**What it is.** Thread turns a team's scattered communication (email, issues, docs, chat) into a small set of *facts* — "The Pro plan costs $35 per month" — stores them with a vector embedding, and answers questions by retrieving the right facts and writing a short cited answer. When a newer fact replaces an older one, the older one is marked stale rather than deleted, so "what did we decide before?" stays answerable.

**What it runs on.** Not Azure. See section 2 for the exact list; in one line: a Python/FastAPI app on **Vercel** (Tokyo), a **Supabase** Postgres with **pgvector** (AWS Tokyo), **GitHub Actions** as the scheduler and CI, **Clerk** for sign-in, **Composio** for connectors, and **Gemini / OpenRouter** for language models and embeddings.

**What was achieved, in numbers** (3 benchmark runs, synthetic company, detail in section 12):

| | Result |
|---|---|
| Retrieval hit@5 | 1.000 (45 questions × 3 runs) — on a small corpus, so a ceiling, not scale evidence |
| Answers correct / citations real and supportive | 135/135 · 143/143 |
| Unanswerable questions correctly refused | 30/30 |
| Deciding "this new fact replaces that one" (F1) | **0.85**, against 0.12 for the previous method |
| Changing-fact chains left in the correct state | **41/42**, against 0/42 with the step switched off |

---

## 1. What the system does, by example

Follow one fact through the system. A teammate writes in an email: *"Heads up — the Pro plan is going from $29 to $35 per month."*

1. **Fetched.** The scheduled sync asks Composio for new Gmail messages. The email arrives as an *episode* (source `composio:gmail`, body truncated to 2,000 characters, `visibility = private`, owned by whoever connected the mailbox).
2. **Stored once.** The body is hashed. If that exact content was ingested before, it is skipped. Otherwise it is written to the `episodes` table.
3. **Distilled.** An LLM reads the episode and returns structured facts: subject `Pro plan`, predicate `price changed to`, object `$35 per month`, one-line summary, confidence 0.9.
4. **Embedded.** The summary becomes a 768-number vector.
5. **Reconciled.** The new vector is compared with existing facts in the same team. "The Pro plan price was raised to $29 per month." scores ~0.9 similar. An LLM judge is shown the pair and answers `supersedes`.
6. **Saved.** The new fact is stored; the $29 fact gets `invalidated_at = now()`; a `supersedes` edge links them.
7. **Asked.** Later someone asks "What is the current price of the Pro plan?" The question is embedded, vector search and full-text search run, their rankings are fused, stale facts are filtered, and the top five facts go to a model that writes: *"The Pro plan price is now $35 per month [fact-id]."*
8. **Audited.** The search is appended to a ledger: who asked, what, which facts came back.
9. **History.** Asking for the *history* of Pro plan pricing returns all versions in time order, each marked `valid` or `stale`.

That path — fetch, dedupe, distil, embed, reconcile, retrieve, answer — is the whole product.

---

## 2. What it runs on (and the Azure question)

### 2.1 Azure

**The project does not use Azure.** I searched the repository for it. The only matches are in the web app's front-end file: a "Sign in with Microsoft" button (which asks Clerk for its Microsoft login provider, internally named `azure`), and a logo entry for Azure in the catalogue of apps users may connect. Neither involves hosting, storage, compute, or any Azure service.

If you were told or assumed it runs on Azure, the actual infrastructure is below. If you want to *move* it to Azure, a mapping is in section 2.4 — that is a suggestion, not the current state.

### 2.2 What it actually runs on

| Layer | Service | Where it is configured | What it does here |
|---|---|---|---|
| **Compute (API + web app)** | **Vercel** serverless Python | `vercel.json`, `api/index.py` | Serves FastAPI and the single-page web app. Region `hnd1` (Tokyo). `maxLambdaSize` 50 MB. All routes go to `api/index.py` |
| **Database** | **Supabase** Postgres 16 + **pgvector** | `DATABASE_URL` (pooler host `aws-0-ap-northeast-1`, port 6543) | Facts, episodes, vectors, full-text index, accounts, ledger, billing tables. Transaction-mode pooler. Same region (Tokyo) as the compute |
| **Scheduler** | **GitHub Actions** | `.github/workflows/sync.yml` | Runs `scripts/db/sync_once.py` every 15 minutes (cron `*/15 * * * *`), 20-minute timeout, one run at a time, secrets from the `Production` environment |
| **CI/CD** | GitHub Actions | `ci.yml`, `e2e.yml` | Lint, type-check, unit, integration, RLS check, isolation suite, secret scan, dependency scan, Playwright E2E |
| **Sign-in** | **Clerk** | `CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY` | Issues sign-in sessions (RS256 JWTs). The app verifies them against Clerk's public keys; it never sees passwords |
| **Connectors** | **Composio** | `COMPOSIO_API_KEY`, `brain/core/connectors/composio.py` | One API in front of Gmail, GitHub, Calendar, Notion, Slack and others; handles each app's OAuth |
| **Language models** | **Gemini** (direct) and **OpenRouter** | `brain/core/llm.py` via LiteLLM | Extraction, reconcile judge, answer writing. Currently routed to OpenRouter (see 2.3) |
| **Embeddings** | **Gemini** `gemini-embedding-001` (768-dim), OpenRouter fallback | `brain/core/embed.py` | Meaning vectors for facts and queries |
| **Cache (optional)** | **Redis** | `REDIS_URL` | Query-result cache and job queue. **Not provisioned in the hosted deployment**; everything degrades without it |
| **Errors** | **Sentry** (optional; **not active on the hosted deployment**: not installed there and initialised only in `main()`) | `SENTRY_DSN` | Error tracking with a scrubber that removes query, answer, email, tokens |
| **LLM tracing** | **Langfuse** (optional; **not active and currently broken**: calls `.trace()`, absent in SDK 4.x, and never flushes) | `LANGFUSE_*` | Intended to record each model call. See [SYSTEM_BOUNDARIES.md](SYSTEM_BOUNDARIES.md) |
| **Metrics** | **Prometheus** text at `/metrics` | `brain/core/metrics.py`, `monitoring/alerts.yaml` | Latency histogram, token and cost counters, cache hit rate |
| **Local dev** | **Docker** (`pgvector/pgvector:pg16`) | — | The local database used for development and the benchmark |

### 2.3 Which models are used right now

Set in `.env` (not committed):

| Job | Model | Why |
|---|---|---|
| Extraction, reconcile judge | `openrouter/google/gemini-2.5-flash-lite` | Cheapest model that does structured JSON well; $0.10 in / $0.40 out per million tokens |
| Answer writing | `openrouter/google/gemini-3.8-flash` | Better prose; $0.825 in / $4.125 out per million tokens |
| Benchmark answer judge | `openrouter/openai/gpt-4o-mini` | A different model family from the answerer, to avoid a model grading itself |
| Embeddings | `gemini-embedding-001` | Direct Gemini; same model via OpenRouter if Gemini rate-limits |

If no model is named, `brain/config.py` picks from whichever API keys are present (OpenRouter free model → Moonshot → Grok → Anthropic → OpenAI → Gemini). `TASK_ROUTES` supplies a primary and fallback per task, but **when a call names a model explicitly the fallback is that same model** — which is how the pipeline currently runs.

### 2.4 If you wanted this on Azure (suggestion only)

| Today | Azure equivalent |
|---|---|
| Vercel serverless | Azure Container Apps (or App Service) running the FastAPI container |
| Supabase Postgres + pgvector | Azure Database for PostgreSQL – Flexible Server, with the `vector` extension enabled |
| Supabase pooler | PgBouncer built into Flexible Server |
| GitHub Actions cron | GitHub Actions still works, or an Azure Container Apps Job / Functions timer |
| Redis | Azure Cache for Redis |
| Secrets in env vars | Azure Key Vault + managed identity |
| Gemini / OpenRouter | Azure OpenAI (chat + `text-embedding-3` — **a different embedding space, so every fact would need re-embedding and the index rebuilt**) |
| Clerk | Clerk still works; or Microsoft Entra ID |
| Sentry / Prometheus | Application Insights / Azure Monitor |

The code is database-agnostic only in the sense that it is plain Postgres + pgvector; the Supabase-specific parts are the `authenticated` role grants and the pooler URL.

---

## 3. System picture

```
                                     ┌────────────────────────────┐
                                     │   Sources (via Composio)   │
                                     │ Gmail · GitHub · Calendar  │
                                     │ Notion · Slack · …         │
                                     └──────────────┬─────────────┘
                                                    │ poll every 15 min
                                      ┌─────────────▼──────────────┐
   GitHub Actions cron ──────────────▶│  scripts/db/sync_once.py      │
                                      │  → Brain.sync()            │
                                      └─────────────┬──────────────┘
                                                    │ episodes
┌──────────┐   ┌──────────────┐        ┌────────────▼─────────────┐        ┌────────────────────┐
│ Web app  │   │  MCP server  │        │      brain.engine.Brain  │        │   LLM providers    │
│ (SPA)    │──▶│  (Claude     │───────▶│  (single public API)     │◀──────▶│ OpenRouter/Gemini  │
│ Slack bot│   │  Code/Cursor)│        └─┬──────────┬───────────┬─┘        │  via litellm       │
│ CLI      │   │  + hooks     │          │          │           │          └────────────────────┘
└────┬─────┘   └──────────────┘     ingest.py   extract.py   retrieve.py          ▲
     │ HTTPS                            │          │  ▲         │                  │ every call
┌────▼──────────────┐                   │       reconcile.py    │            brain/core/llm.py
│ Vercel serverless │                   │          │            │            (retry · breaker ·
│ FastAPI           │                   ▼          ▼            ▼             metering · fallback)
│ api/index.py      │            ┌───────────────────────────────────┐
└────┬──────────────┘            │  brain/core/db_pg.py (all SQL)    │       ┌────────────────────┐
     │ Clerk JWT                 │  Supabase Postgres 16 + pgvector  │◀──────│ embed.py           │
     ▼                           │  episodes · facts · fact_edges    │       │ Gemini embeddings  │
   Clerk                         │  ledger · accounts · usage …      │       │ (+ OpenRouter      │
                                 └───────────────────────────────────┘       │  fallback)         │
                                                                             └────────────────────┘
```

Two rules hold the structure together: **every model call goes through `llm.py`**, and **every SQL statement goes through `db_pg.py`**. Token metering, cost caps, bring-your-own-key routing, circuit breaking and tenant scoping are therefore written once.

---

## 4. How a request is served on Vercel

1. The browser calls an endpoint. Vercel routes *every* path to `api/index.py`, which puts the project root on `sys.path` and imports `brain.wrappers.api.app`.
2. **Cold start.** The first request in a fresh instance imports the app. `brain/auth.py` runs checks at import time and **refuses to start** if auth bypass is on in production or if production has no Clerk secret. A hosted environment (Vercel) is treated as production unless `BRAIN_ENV` says otherwise, so a Preview build cannot accidentally serve the app unauthenticated.
3. **Database pool.** On Vercel the pool opens zero idle connections and allows at most three (`BRAIN_PG_MAXCONN`), because each invocation may be its own short-lived process. Elsewhere it keeps two warm and allows ten. Every connection is validated (`SET statement_timeout = 0`, commit) before it is handed out; dead ones are discarded.
4. **Schema.** The startup migration thread is *skipped* on serverless. However, `Brain.__init__` calls `db.migrate()` once per process, so the first authenticated request in each cold instance runs the (idempotent) schema statements — several dozen round-trips. This is a known cost.
5. **Middleware order.** Trace middleware (reads or creates `X-Trace-Id`, records latency into the metrics store, writes a structured JSON log line) → security-headers middleware (nosniff, frame-deny, HSTS, a content-security policy) → CORS.
6. **Auth.** `get_identity` extracts the bearer token and resolves it to an `Identity` (section 7).
7. **Brain.** `_brain(identity)` builds a `Brain` for that workspace/team/caller/principals, resolves the user's `account_id` (cached in-process), sets the RLS session variable, and sets the billing context so every model call writes a usage row against the right account.
8. **Handler runs**, errors are converted to clean JSON. Any 5xx has its detail logged and replaced with an opaque message so database error text never reaches a client.
9. The connection returns to the pool.

---

## 5. The data model

All tables are created by `db_pg.migrate()` (runtime) and, for production governance, by Alembic migration `0001` (`0002`–`0005` cover the embedding migration and a plan backfill). Two sources of schema truth is a drift risk; CI exercises `migrate()`, not Alembic.

### 5.1 The knowledge tables

| Table | Key columns | Purpose |
|---|---|---|
| **episodes** | `id`, `workspace_id`, `source`, `external_id`, `title`, `body`, `author`, `url`, `occurred_at`, `content_hash`, `acl`, `team_id`, `extracted` (0/1), `owner_id`, `visibility`, `supersedes_episode_id`, `ingest_trigger` | The raw source item. Unique on `(workspace_id, content_hash)` — this is the deduplication |
| **facts** | `id`, `workspace_id`, `subject`, `predicate`, `object`, `summary`, `episode_id`, `acl`, `confidence`, `introduced_at`, `invalidated_at`, `source_kind`, `team_id`, `visibility`, `owner_id`, `fact_vec_768 vector(768)`, `fts_vec tsvector` (generated) | **The only table retrieval reads.** `invalidated_at IS NULL` means the fact is current |
| **fact_edges** | `src_fact`, `dst_fact`, `type` (`supersedes` / `in_tension_with`), cascading foreign keys | Relations between facts |
| **ledger** | `user_id`, `ts`, `query`, `surface`, `fact_ids`, `context_sent`, `team_id` | Append-only record of every search |
| **teams**, **team_kpis** | name, `mission`, KPI name/target/current/unit/period | A team's goals, injected into the answer prompt |

Constraints that matter: `confidence` between 0 and 1; `source_kind` in (`llm`, `human`, `agent`, `ai-tool`, `integration`); `visibility` in (`private`, `team`, `shared`); ledger `surface` in a fixed list.

### 5.2 Indexes

| Index | Type | Used by |
|---|---|---|
| `facts_vec_768_idx` | **HNSW** (m=16, ef_construction=64), cosine | vector search. Built **out of band** by `scripts/db/rebuild_index.py` because building it locks the table; `/readyz` reports it missing |
| `facts_fts_gin` | GIN on `fts_vec` | full-text search |
| `facts_ws_idx`, `facts_owner_idx` (partial) | b-tree | workspace and owner scoping |
| `episodes_ws_hash` (unique), `episodes_ext_id`, `episodes_supersedes` | b-tree | dedup, "same external item changed" lookup |
| `ledger_ws`, `ledger_team` | b-tree | ledger paging |

### 5.3 Account, billing and operations tables

`accounts` (individual/company), `memberships` (user ↔ account, role owner/admin/employee, status), `invitations` (tokenised, expiring), `conversations` + `messages` (private chat history), `usage_events` (tokens and cost per call — never the text), `integration_usage` (connector API calls per day), `plans` (free 500 credits/1 seat · team 5,000/20 seats/$49 · business 50,000/100 seats/$299), `subscriptions`, `workspace_llm_keys` (customer keys, Fernet-encrypted), `connector_sync_state` (per-source watermark and failure backoff), `connector_webhook_routes`, `webhook_subscriptions`, `dead_letter_jobs`, `slack_workspace_integrations`, `slack_user_links`, `slack_channel_mappings`.

---

## 6. The data flows in detail

### 6.1 Ingestion — `brain/core/ingest.py`

Input: a list of episode dicts from a connector. For each one:

1. **Worthiness filter.** Dropped if the body is shorter than 12 characters, contains a noise phrase ("unsubscribe", "has joined the channel", …), or is a short acknowledgement ("sounds good", "lgtm", "+1", "ok", …).
2. **Injection scrub.** Eight regular expressions replace phrases like "ignore previous instructions", `<system>`, "you are now …", "new instructions:" with `[filtered]`, *before* the text is hashed or stored.
3. **Content hash.** SHA-256 of the scrubbed body. If `(workspace, hash)` already exists, skip — this is why re-syncs and webhook replays never duplicate.
4. **Update detection.** If the same `external_id` was seen before with a *different* hash, the new episode records `supersedes_episode_id` pointing at the old one.
5. **Insert** with `ON CONFLICT DO NOTHING`; commit per episode. Ownership (`owner_id`, `visibility`) travels with the episode.

Output: a count of newly inserted episodes.

### 6.2 Connectors — `brain/core/connectors/composio.py`

Per connected app, a fixed list of actions is executed (Gmail: `GMAIL_FETCH_EMAILS`, up to 100 emails, `category:primary`; GitHub: repositories and issues, 50 per page; Calendar, Drive, Notion, Linear, Jira, HubSpot, Slack likewise). Each raw result is normalised by `_normalize_result` into episodes. Items older than the watermark are dropped.

Everything a connector returns is **personal**: each episode is stamped `acl = owner`, `owner_id = owner`, `visibility = private`, so the facts extracted from someone's mailbox stay private to them. An app with no entry in `_APP_ACTIONS` (Fathom, Apollo and HighLevel today) is silently skipped.

### 6.3 Extraction — `brain/core/extract.py`

`extract.run(conn, ws, limit=20)` takes up to 20 unextracted episodes, oldest first, and for each:

1. Sends the first 6,000 characters to the extraction model with a prompt that asks only for facts "a new employee would need to know six months from now" (decisions, ownership, dates, technical choices, process rules), and explicitly not for greetings, admin events or duplicates. The episode text is wrapped in `<untrusted_content>` tags with a system instruction never to obey anything inside.
2. Validates the JSON against a Pydantic model (`subject`, `predicate`, `object`, `summary`, `confidence` clamped to 0–1, `visibility` forced to `team` or `shared`).
3. Embeds all summaries in one batch.
4. For each fact: reconcile (6.4), then save.
5. Marks the episode `extracted = 1` **only if everything succeeded.** An empty model response (provider down or breaker open) or a validation failure leaves it at 0 so the next run retries — an outage cannot permanently discard content.

Privacy rule applied at save time: the *episode* decides visibility, not the model. If the episode is private, the fact is private whatever the model said.

`extract_from_text` is the same flow for text that never became an episode — an AI assistant's reply — with a stricter prompt and a confidence floor of 0.7. `record_human_fact` stores a person's statement with confidence 1.0. `correct_fact` invalidates a fact and records its replacement with a `supersedes` edge.

### 6.4 Reconciliation — `brain/core/reconcile.py` (new)

This is the step that makes "what did we decide before?" work.

**Candidate search.** The new fact's vector is compared (cosine, pgvector) with valid facts in the *same team*; a private fact is only considered if its owner is the same person. Up to `RECONCILE_K = 5` candidates with similarity ≥ `RELATED_THRESHOLD = 0.70` are kept.

**Judge.** One LLM call is shown the new fact (told it is the most recent statement) and the candidates, and returns a verdict per candidate:

| Verdict | Meaning | Effect |
|---|---|---|
| `duplicate` | same claim, same value | the new fact is **not stored** |
| `supersedes` | updated value of the same thing | new fact stored; old fact gets `invalidated_at`; `supersedes` edge |
| `contradicts` | conflicts, but not clearly a replacement | both valid; `in_tension_with` edge |
| `unrelated` | different thing | nothing |

Real example from this project (judge output):

```
new:  "The launch has moved to July 10th."
cand: a1  "The launch is scheduled for July 5th."      → supersedes
cand: b2  "The API is deployed on Vercel."              → unrelated
```

**Why embeddings alone are not enough.** "Launch is July 5th" and "July 10th" embed almost identically. Similarity can tell two facts are *about the same thing*; it cannot tell whether one *replaces* the other. That needs reading, hence the judge.

**Modes.** `BRAIN_RECONCILE_MODE=enforce` (default) applies decisions. `shadow` runs the whole step but only logs what it would have done to `reconcile_shadow_log`, applying nothing, so supersession can be audited on real data before it is trusted. `off` skips the step entirely.

**Failure behaviour.** If the judge call fails or returns nothing, the code falls back to the previous rule — an exact-subject near-duplicate check — and never invalidates anything on a guess. The returned decision is applied inside the save: `reconcile.apply` invalidates and links.

### 6.5 Retrieval and answering — `brain/core/retrieve.py`

`Brain.search(query)` → `retrieve.run` → `retrieve.format_answer`:

1. **Embed the query** (`RETRIEVAL_QUERY` task type) through an in-process 256-entry cache.
2. **Redis lookup** if `REDIS_URL` is set, keyed on the embedding **plus** workspace, principals, team, flags — scope is part of the key so one tenant is never served another's results. Skipped silently without Redis.
3. **Vector search**: top 30 by cosine distance, in the workspace, valid facts only.
4. **Full-text search**: top 30 by `ts_rank` using `plainto_tsquery('english', …)`.
5. **Fuse** with Reciprocal Rank Fusion: score = Σ 1/(60 + rank). Facts found by both lists rise.
6. **Hydrate** all candidates in one query (an earlier per-fact version took ~7 s for 37 candidates against a remote database).
7. **Filter**: ACL (the caller's principals must intersect the fact's ACL, or the ACL is `everyone`); visibility (`private` → owner only, even for admins and cross-team searches; `shared` → everyone; `team` → same team); stale facts dropped unless history was asked for.
8. **Order**: ordinary questions sort valid before stale, then by fusion rank, then optional cross-encoder rerank of the top 30 (off by default). Questions containing recency words ("latest", "recent", "today", "newest", "this week"…) instead merge in the 20 newest facts and sort by time. *"current" is deliberately not a recency word* — it asks which fact is still valid, which supersession answers.
9. **Truncate** to 8 facts; **log** to the ledger; **cache** for 300 s.
10. **Answer** (`format_answer`): if there are no facts, or the top confidence is under 0.35, or every fact is stale, return "I don't have reliable information about that in company memory." Otherwise surface any `in_tension_with` pairs, fetch the team's mission/KPIs, and ask the answer model — given the top 5 facts and instructions to use only those facts, be 1–3 sentences, and cite fact IDs in brackets — to write the answer. A guardrail then scans it: queries that look like jailbreaks ("show your system prompt", "print all API keys") and answers that contain secret-shaped strings (API-key prefixes, `postgresql://…`, "system prompt") are replaced with "I can't help with that." If the model is unreachable, the facts are returned without prose and the response says so.

**Output shape** (a real response from this project):

```json
{
  "answer": "From company memory:",
  "synthesized_answer": "Postgres was chosen over DynamoDB because the company needs relational joins and pgvector for search [f9b33153-…].",
  "facts": [
    {"summary": "We chose Postgres over DynamoDB because …", "status": "valid",
     "confidence": 1.0, "source": "human", "fact_id": "f9b33153-…",
     "team": "engineering", "visibility": "team", "episode_id": null}
  ],
  "conflicts": []
}
```

Note: `"From company memory:"` is the success header. The prose is in `synthesized_answer`; a failed synthesis says `"(synthesis unavailable)"`.

### 6.6 Decision history

`Brain.decision_history(subject)` runs the same retrieval with `include_invalid = True` on the query "decisions about {subject}" and returns the facts sorted by `introduced_at`. A real result for the price chain:

```
stale  | The Pro plan costs $20 per month.
stale  | The Pro plan price was raised to $29 per month.
valid  | The Pro plan price is now $35 per month.
```

Before the fixes in section 12.6, this view could never show a stale fact: both searches filtered them out in SQL, and the ranking pushed them below valid facts before truncation.

### 6.7 Scheduled sync (what actually keeps data fresh in production)

A serverless host cannot run a long-lived worker, so **GitHub Actions** is the scheduler. Every 15 minutes `sync.yml` checks out the repo, installs dependencies, fails fast if `DATABASE_URL`, `COMPOSIO_API_KEY` or `GEMINI_API_KEY` is missing, and runs `scripts/db/sync_once.py`. For each workspace holding episodes it:

1. reads the watermark from `connector_sync_state` and marks the source `running`;
2. calls `Brain.sync("composio", since=watermark)` — fetch → ingest → extract (with reconcile);
3. on success advances the watermark to now and resets the failure count; on failure records the error and an exponential backoff (`min(2^failures × 60, 3600)` seconds) and **does not advance the watermark**, so the same window is refetched next time.

One workspace failing does not stop the others; the process exits non-zero if any failed. A `concurrency` group prevents two syncs overlapping.

### 6.8 The push path (webhooks, queue) — not live in the hosted deployment

Code exists for push ingestion: `POST /api/ingest/event` verifies an HMAC signature (`x-composio-signature`), de-duplicates by delivery id in Redis, maps the connected-account id to a workspace through `connector_webhook_routes`, and enqueues an `arq` job; `brain/worker.py` has jobs for extraction, per-source sync, a webhook fast path, cron polling and a dead-letter table. All of it needs Redis and a long-lived worker. **The hosted deployment has neither**, so the webhook endpoint answers 503 and `/api/sync` falls back to a background thread (which a serverless host may freeze after the response). The scheduled sync above is the real path.

### 6.9 Other ways in and out

| Surface | How it works |
|---|---|
| **Web app** (`brain/wrappers/webapp/index.html`, ~2,900 lines, one file) | Loads Clerk's JS, gets a short-lived session token before each call, shows a chat view, history, a team selector, integrations hub, members/invites, settings (mission and KPIs), usage. Also "Try live demo" (guest session) |
| **REST API** (`brain/wrappers/api.py`, ~2,900 lines) | 64 endpoints plus 3 Slack routes. Connecting an app (`/api/composio/connect`) seals the caller's identity into a signed, 15-minute `state` that the callback must return, so the browser redirect is trusted only if the signature verifies. Endpoints: search, history, record, correct, ledger, facts, teams and goals, composio apps/connect/callback, auth/signup, SSO provisioning, conversations, invites and members, plans/subscription/usage, dead-letter admin, Slack OAuth and mappings, account export, platform metrics, `/healthz`, `/readyz`, `/metrics`, `/api/auth-config` |
| **MCP server** | Tools: `search_company_facts`, `get_decision_history`, `record_fact`, `correct_fact`, `harvest_from_response`, `list_teams`. Lets Claude Code or Cursor read and write memory |
| **Claude Code hooks** | On every prompt, search memory and inject relevant facts as extra context (logged as `hook-inject`); after every reply, extract company facts from the response and store net-new ones (`hook-harvest`). Never blocks the user |
| **CLI** | `brain backfill/sync/search/history/record/correct/log/teams/team-create/eval-providers` |
| **Slack bot** | Events API with signature check and 5-minute replay window; answers @mentions and DMs in a background thread; links Slack users to accounts by email. Slack *ingestion* was removed; the bot only answers |

---

## 7. Identity, permissions and privacy

### 7.1 Who is calling

Every request resolves to an `Identity` (user id, email, workspace, team, principals, role):

| Mode | When | How |
|---|---|---|
| **Clerk** | normal | Bearer token → verified RS256 against Clerk's JWKS (URL derived from the publishable key), expiry checked. Role, workspace and team come **only** from the token's `public_metadata` (writable by the backend alone). `unsafe_metadata`, which the signed-in user can edit from the browser, is ignored |
| **Guest** | "Try live demo" | `POST /api/guest-session` mints `guest.<payload>.<HMAC-SHA256>` valid 4 hours, locked to workspace `ws_demo`, read-only. Signed with `GUEST_TOKEN_SECRET`, or a BLAKE2b key derived from `BRAIN_KEY_ENCRYPTION_KEY` |
| **Dev bypass** | local only | Only when not production *and* (no Clerk secret *or* `BRAIN_DEV_BYPASS=true`). Server exits if it is on in production |

`require_admin` (role admin/owner), `require_not_guest` (blocks every write and settings call for guests) and `require_non_demo_workspace` are dependency checks on endpoints. Guest limits: 100 questions per rolling 24 hours across all guests (enforced on the server, in process memory) and 20 token requests per minute per IP. A 20-question session cap is sent to the browser and enforced there only, not on the server.

### 7.2 Who can see a fact

A fact is returned only if **all** hold: its workspace matches; the caller's principals intersect its ACL (or ACL is `everyone`); and its visibility allows it — `private` to its owner alone (not even `cross_team` admins), `shared` to everyone, `team` to the same team. The same rules are applied inside the reconcile candidate query, so a private fact is never shown to the judge on behalf of someone else.

### 7.3 Defence in depth

- **Row-level security** is enabled on episodes, facts, fact_edges, ledger, teams, team_kpis (workspace policy), plus an owner-only policy on private facts and *forced* user-scoped policies on conversations and messages. `scripts/db/check_rls.py` is a CI gate that fails the build if a required table lacks RLS.
- **Caveat on RLS.** Policies on the knowledge tables are `ENABLE`d, not `FORCE`d, and the application connects with one database role. A table's owner role bypasses non-forced policies, so for those tables correctness rests on the explicit `workspace_id` predicates in every query (they are there) with RLS as a second layer — I did not verify the production role's attributes. The chat tables are different: they are forced, and requests drop to the `authenticated` role.
- **Customer API keys** are Fernet-encrypted (AES-128-CBC + HMAC) under `BRAIN_KEY_ENCRYPTION_KEY`; a missing or wrong master key makes decryption return an empty string rather than ciphertext.
- **Signed OAuth state.** The integration callback is a bare browser redirect with no bearer token, so it cannot be authenticated the usual way. `auth.sign_state` / `verify_state` seal `{user_id, workspace_id, account_id, expiry}` with an HMAC (key derived with BLAKE2b from `BRAIN_KEY_ENCRYPTION_KEY`, falling back to `CLERK_SECRET_KEY`); the callback reads identity from the verified state and refuses anything missing, forged or expired.
- **Signup is authenticated.** `POST /api/auth/signup` requires a verified, non-guest session and uses that session's user id; the `password` field in the body is ignored.
- **Per-request billing context.** The context that attributes each model call to an account is held in a `ContextVar`, so concurrent requests on a thread pool cannot overwrite one another.
- **Prompt-injection** is handled three ways: scrub at ingest, `<untrusted_content>` tags with an explicit instruction at extraction and answering, and the output guardrail.
- **HTTP**: security headers, CORS (defaults to `*` unless `ALLOWED_ORIGINS` is set), 5xx detail never returned, per-user rate limits (60/min LLM endpoints, 120/min writes, 20/min auth).

---

## 8. Resilience — what happens when something is down

| Failure | Behaviour |
|---|---|
| Chat model down or rate-limited | 3 attempts with exponential backoff and jitter; then the fallback model; a circuit breaker (5 failures → open 30 s) makes later calls fail fast. Answers degrade to "facts without prose"; extraction leaves episodes unextracted for retry |
| Reconcile judge unavailable | Falls back to the exact-subject duplicate check; invalidates nothing |
| Gemini embeddings failing | Breaker opens after 3 failures; calls go through the same model on OpenRouter; if that also fails, extraction and harvest **refuse to save** (the episode is left unextracted and retried on the next sync). Manually recorded facts are the exception: they can still be stored with a zero vector |
| Redis absent or down | Every cache and queue call is a silent no-op |
| Reranker missing | Skipped |
| Database connection dead | Validated on checkout, discarded and replaced (up to 3 tries) |
| Connector failing | Failure recorded, backoff applied, watermark not advanced |
| Credit limit reached | HTTP 402 before the model call (unless the account brings its own key) |

`/healthz` is liveness only. `/readyz` checks database, Redis, an LLM key, configuration problems (unset workspace, no embedding key, no encryption key, bypass in production) and the HNSW index, each with a 2-second timeout, and returns 503 if any fail. `/api/status` exposes breaker states.

---

## 9. Cost metering

Every model call writes a `usage_events` row — account, user, team, task, model, prompt and completion tokens, cost — never the text. Cost is tokens × the per-1K price in `MODEL_PRICES`; **models not in that table (including the OpenRouter ones currently used) are billed at the `default` rate**, so recorded cost is only approximate for them. One credit = $0.001. Plans cap monthly credits; bring-your-own-key accounts are uncapped. `GET /api/usage` aggregates by task and team.

---

## 10. Observability

Structured JSON logs with a per-request trace id (`X-Trace-Id`), Prometheus metrics at `/metrics` (request count, errors, latency histogram with buckets 0.05–10 s plus p50/p95/p99 from a sample window, LLM tokens and cost, Composio calls, cache hits and misses, facts extracted, queue depth), alert rules in `monitoring/alerts.yaml` (`error_rate_spike`, `p95_latency_breach`, `queue_backlog`, `cost_threshold`, `composio_threshold`, `auth_failure_spike`), optional Sentry with a PII scrubber, optional Langfuse traces of every model call. Metrics counters live in process memory, so on serverless they reset with each instance.

---

## 11. Build, test and release

**Tests.** 357 test functions in 21 files. The last full local run, 2026-10-22 (Playwright and load suites excluded): **354 passed, 7 skipped, 0 failed.** The benchmark gate skips, with a reason, whenever the embedding providers are unavailable; the Playwright and load suites need `playwright`/`locust`, which are not installed here. The full re-audit is in `docs/audit-report.md`. Suites: unit (fusion, ACL, extraction, validation, auth, guardrails), integration against live Postgres, **isolation** (tenant isolation, RLS, privilege escalation — must be 100%), privacy, metering, sync pipeline, the reconcile tests, regression tests for the security fixes, an architecture-guard class, and Playwright journeys plus Locust/k6 load scripts.

**CI** (`ci.yml`, every push and PR to `main`): lint (ruff), type-check (pyright, non-blocking), unit tests, integration tests with Postgres and Redis services plus the RLS check, the isolation suite as a hard gate, secret scan (gitleaks), dependency audit. `e2e.yml` runs Playwright against a live server.

**Release.** Pushing to `main` deploys to Vercel; secrets are scoped per environment. `scripts/` holds operations: index rebuild, re-embedding, RLS and index verification, query plans (`explain_queries.py` fails if a hot query sequentially scans `facts`), smoke test, seeding.

---

## 12. Benchmarks — everything measured

### 12.1 How the benchmark works

`evals/bench.py` runs on a **synthetic company ("Meridian Labs")** in `evals/corpus.yaml` so anyone can reproduce it, with no personal data. Ground truth is written by reading the corpus — never derived from what retrieval returned (an earlier harness did that, which made recall impossible to lower; it was withdrawn).

| Component | Size |
|---|---|
| Knowledge base | 44 facts |
| Answerable questions | 45 — 32 direct, 13 deliberately paraphrased with little word overlap — each with gold fact ids |
| Unanswerable questions | 10 |
| Changing-fact chains | 14 chains, 30 versions, 16 updates |
| Decoys (resemble an existing fact, must replace nothing) | 14 |
| Paraphrases (should be detected as duplicates) | 8 |
| Future-dated changes (current value must stay valid) | 2 |

Three parts: **A** retrieval and answer quality; **B** supersession decisions, one at a time against a database in its true state, for three methods; **C** the same facts fed through the real save pipeline, then queried, with reconciliation on and off. The benchmark refuses to run against a non-local database (it deletes `ws_bench_*`), and aborts if any embedding call fails rather than scoring zero vectors.

**Method:** 3 full runs on 2026-10-21; mean with range; counts pooled. Models: extraction and judge `gemini-2.5-flash-lite`; answers `gemini-3.8-flash`; answer judge `gpt-4o-mini`; embeddings `gemini-embedding-001`.

### 12.2 Retrieval (k = 5)

| Ranker | Hit@5 | Recall@5 | Precision@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|
| Vector only | 1.000 (135/135) | 1.000 | 0.209 | 0.982 | 0.986 |
| Full-text only | 0.244 (33/135) | 0.244 | 0.049 | 0.244 | 0.244 |
| Hybrid (RRF) | 1.000 (135/135) | 1.000 | 0.209 | 0.982 | 0.986 |
| Production path | 1.000 (135/135) | 1.000 | 0.209 | 1.000 | 1.000 |

Reading it: with 44 facts and k=5 the system returns 11% of the corpus per query, so a perfect hit rate is expected and says little about scale. Precision is capped near 0.2 because most questions have one relevant fact. Full-text alone finds the right fact for only 24% of questions because `plainto_tsquery` ANDs its terms ("what mobile framework did we choose" cannot match "React Native for the mobile app"); semantic search carries the system. The production path scored slightly higher MRR than raw RRF; I did not isolate why.

### 12.3 Answer quality

| Metric | Result |
|---|---|
| Answer contains the expected content | 135/135 |
| Faithfulness (LLM judge) | 0.956 (129/135) |
| Citations that point at a retrieved fact | 143/143 |
| Citations judged to support their sentence | 143/143 |
| Unanswerable questions refused | 30/30 |
| Answer latency p50 / p95 | ≈ 4.0 s / ≈ 13.8 s |
| Retrieval latency p50 / p95 | ≈ 2.3 s / ≈ 3.6 s |

The six unfaithful answers were two questions (q33, q44) in each run where the model added a caveat such as "the facts do not explicitly specify …"; the judge counted the caveat as an unsupported claim. Abstention is detected by phrase match on the answer, not by a judge. Latency is dominated by network calls to the embedding and model providers from a development machine and says nothing about a co-located deployment. (Earlier measurement against remote Supabase: query embedding ≈ 1.2 s, vector search ≈ 0.23 s, full-text ≈ 0.23 s — the embedding round-trip costs about five times both database queries combined.)

### 12.4 Supersession decisions (pairwise)

| Metric | Exact-subject (previous) | Embedding similarity only | Embedding + LLM judge (current) |
|---|---|---|---|
| Precision | 1.000 | 0.385 | **0.855** |
| Recall | 0.062 (3/48) | 0.625 (30/48) | **0.854** (41/48) |
| F1 | 0.118 | 0.476 | **0.852** |
| Real updates lost as "duplicates" | 0/48 | 9/48 (19%) | **0/48** |
| Paraphrases recognised as duplicates | 2/24 | 11/24 | **24/24** |
| Decoys wrongly acted on | 0/42 | 30/42 (71%) | **1/42** |
| Future-dated changes wrongly superseded | 0/6 | 6/6 | 6/6 |
| True predecessor found among candidates | — | — | 47/48 (0.979) |

The previous method almost never fires because it needs the extraction model to recall a fact it was never shown and then matches subject strings exactly. Similarity alone fires too often — it cannot tell an update from a related fact.

### 12.5 End to end through the real pipeline

| Metric | Reconciliation off (previous) | Reconciliation on (current) |
|---|---|---|
| Versions stored | 85/90 | 90/90 |
| Version marked valid/stale correctly | 37/90 (41%) | **89/90 (99%)** |
| Chains ending in the correct state | **0/42** | **41/42** |
| "What is the current X?" ranks the latest first | 7/42 (17%) | **41/42 (98%)** |
| …and the answer states the latest only | 0/42 | **41/42** |
| History returns every version | 85/90 | 90/90 |
| History marks stale/valid correctly | 37/90 | **89/90** |
| Unrelated facts wrongly invalidated | 0 | 0 |

With reconciliation off, the old behaviour also silently dropped some real updates as near-duplicates (5 of 90 versions never stored).

### 12.6 What the benchmark found and fixed

1. **History could never show a replaced fact** — vector and full-text SQL excluded invalidated facts and the ranking sank them; both now honour `include_invalid`.
2. **"current" in a question switched retrieval into a time-sorted mode** that buried the topical answer, so "what is the current price?" abstained. "current" is no longer a recency trigger.
3. **An embedding rate-limit error printed the API key into logs, and a failed embedding silently stored a zero vector.** Errors are now redacted and there is an OpenRouter fallback for the same model.
4. **The candidate threshold** was lowered from 0.75 to 0.70 after 0.75 missed 5 of 16 true updates when embeddings came from the fallback route (true updates scored 0.725–0.983).
5. A flaky dedup test (it depended on a live model naming a fact identically twice) was made deterministic.

### 12.7 Known weaknesses (measured, not hidden)

- **Future-dated changes are mishandled.** "The Team plan *will* cost $89 starting January 1" invalidates today's $79 (6/6 for both model-based methods).
- **7 of 48 updates missed** — always the same few: AWS → Google Cloud, React Native → Flutter, Austin → Denver, and once the third Pro-plan price. Changes of provider or place rather than of a number.
- **A decoy was superseded in 1 of 3 runs** ("analytics warehouse runs on PostgreSQL 16" replaced "primary database is PostgreSQL 15").
- The candidate step missed the true predecessor once in 48.

### 12.8 Limits of this evaluation

- The corpus and questions were written by the person who built the system; the paraphrased questions and decoys are meant to counter that, but an independent set would be stronger.
- Chain subjects vary the way an extraction model's do, which favours the new method; the previous method is credited with the extraction hint it needs, so its score is a ceiling.
- The 0.70 threshold was chosen after seeing this benchmark's similarity distribution and has not been validated on held-out data.
- 3 runs, one machine, small n (16 updates, 14 chains) — intervals are wide.
- A few embedding batches came from the OpenRouter fallback when Gemini rate-limited (1 in run 1, 34 in run 3). A separate full run entirely on the fallback gave candidate recall 16/16, supersession precision 0.86 and recall 0.75; its end-to-end answer metrics are excluded because synthesis returned empty responses during that run (`evals/results/bench_fallback_embeddings.json`).

### 12.9 The quality gate and cost

`tests/test_regression.py` scores retrieval against `evals/thresholds.yaml` (hit@5 and recall ≥ 0.90, MRR and nDCG ≥ 0.85, tolerance 0.05 against `evals/baseline.json`); with `BENCH_LLM=1` it also gates answer quality, supersession (recall ≥ 0.80, precision ≥ 0.70, decoy false actions ≤ 0.20) and end-to-end correctness. It needs a local database and an embedding key, so CI runs only its no-database architecture guards. Refresh the baseline with `python -m evals.aggregate --write-baseline`.

Cost: per-run spend was not metered; at OpenRouter's list prices a full run is on the order of cents. For the monthly budget, estimated usage-based cost with Gemini 3.8 Flash for everything is about $2–4 for a light demo and $16–33 for moderate use (3,000 episodes, 3,000 searches); extraction on flash-lite with 3.8 Flash for answers only is roughly $3–5 for the moderate case.

### 12.10 End-to-end benchmark

`evals/e2e.py` runs the whole pipeline from raw messages with the real models: `e2e_corpus.yaml` holds 36 labelled messages (39 claims, 12 updates, decoys, noise, a duplicate, two injection attempts) and 36 questions; a second track runs the same pipeline on a mailbox via Composio and measures extraction groundedness and self-generated-question retrieval with no labels. A preflight refuses to start if a model cannot serve a full-size request, and validity is judged per stage so a provider outage cannot be mistaken for a quality regression. Results, the before/after table and the remaining failures are in the README under "End-to-end benchmark"; in short, on the final code claims captured 38/39, whole update chains correct 11/12, nothing wrongly retired, and 26/28 answers correct end to end (single run).

### 12.11 What has and has not been validated

Everything in this section was measured on the controlled, synthetic benchmark. **No accuracy figure has been measured on a team's real questions or sources yet.** The tooling for doing that is built (`evals/real_eval.py`: export real questions for labelling, score a labelled set with confidence intervals, sample supersession decisions for human review and compute their precision; plus a shadow mode, `BRAIN_RECONCILE_MODE=shadow`, that logs reconcile decisions to `reconcile_shadow_log` without applying them). It has not been run on real data. The protocol, thresholds and commands are in the README under "Validation on real data". Until it is run, treat the figures as evidence that the pipeline behaves correctly and is protected from regressions, not as a production accuracy claim.

### 12.12 Reproduce

```bash
export DATABASE_URL=postgresql://postgres:local@localhost:5433/thread   # local only
python -m evals.bench                  # all parts
python -m evals.bench --parts A --no-answers   # retrieval only, no chat model
python -m evals.aggregate              # tables from evals/results/bench_run_*.json
pytest tests/test_regression.py        # the gate
```

---

## 13. What was built, in order

| When | Change |
|---|---|
| Jun 2026 | First commits; Slack ingestion, MCP server, CLI, hooks, team-scoped facts |
| By end of Aug | Postgres + pgvector, hybrid retrieval, RLS and isolation suite, Clerk replacing Supabase auth (Aug 4), plan caps / bring-your-own-key / credit limits (Aug 6), security hardening (Aug 6), Gemini 768-dim embeddings (migrations 0002–0004), circuit breakers, observability, connection pooling, CI |
| Sep | Slack ingestion dropped for Composio connectors (Sep 3); private-by-owner connector data; team mission and KPIs; scheduler on GitHub Actions instead of a resident worker; hosted-is-production default; guest demo hardening |
| Oct 20–21 | **Semantic supersession** (reconcile step), decision-history fix, "current" fix, embedding fallback and key redaction, synthetic benchmark with 3-run reporting, new quality gate, README and this document |

---

## 14. Issues found while writing this document, and what became of them

Found by reading the code in a review on 2026-10-21. The first group is fixed in the working tree (not committed), each with a test in `tests/test_security_fixes.py`.

### Fixed

| # | Problem | Fix |
|---|---|---|
| 1 | `/api/composio/connect` read `identity.account_id`, which `Identity` does not have, so connecting an integration from the UI returned 500 | The account id is resolved from the membership table (`_resolve_account_id`) |
| 2 | `BACKEND` was no longer exported by `brain.core.db`, so `Brain._plan()`, `_check_backfill_limit()`, `usage_summary()`, three dead-letter admin endpoints and connector metering raised `ImportError`. Visible effects: `GET /api/usage` 500; `POST /api/backfill` ingested episodes then failed before extraction | `BACKEND = "postgres"` restored in `db.py` |
| 3 | `POST /api/auth/signup` was unauthenticated and took the user id from the request body, so anyone could attach an arbitrary user id to an account they created | Requires a verified non-guest session and uses that identity; the body's `password` field is ignored |
| 4 | `/api/composio/callback` trusted `user_id`, `workspace_id`, `account_id` from its query string to write webhook routing rows | Identity travels in a signed, expiring `state` created by the authenticated connect call; missing, forged or expired state returns 400 and writes nothing |
| 5 | Role, team and workspace could be read from Clerk's `unsafe_metadata`, which the user can edit | Only `public_metadata` is trusted. **Effect to know about:** any deployment that relied on `unsafe_metadata` for roles now sees those users as `member` until the role is set in `public_metadata` |
| 6 | The billing context in `llm.py` (and the Composio metering context) was a module-level dict shared across request threads, so concurrent requests could bill the wrong account or read another account's key | Backed by a `ContextVar` (`_ContextDict`), isolated per request/thread |
| 7 | A failed embedding became a zero vector that was saved, creating a fact no query could find | `extract.run` and `extract_from_text` refuse to save and leave the episode for retry (`EmbeddingUnavailable`) |
| 8 | Model calls sent no `max_tokens`, so providers reserved the model's full output limit (65,536 for some models) and refused small requests on a low balance (`402 … can only afford 73`) | Per-task output ceilings in `llm.py` (`extract` 4,000, `reconcile` 1,500, `synthesize` 2,000, …) |
| 9 | `evals/run.py --reset-extracted` assigned `false` to an `INTEGER` column | `extracted=0` |
| 10 | `/readyz` ignored OpenRouter and Moonshot keys when checking for an LLM key; `scripts/db/verify_indexes.py` looked for the dropped index `facts_embedding_hnsw`; `docs/performance.md` described per-fact hydration and the old index name | Corrected |

### Still open

1. **Rate limits and the guest daily cap are in process memory.** On serverless each instance counts separately, so limits are per instance, not global; metrics counters likewise reset per instance. A fix needs a shared store (Redis or a Postgres table), which the hosted deployment does not have.
2. **Content-security policy.** The policy sent with every response allows scripts and connections only from `'self'`, while the page loads Clerk's script from a CDN and calls Clerk's API. I could not open the live site to check this; it should be tested in a browser before changing anything.
3. **Manual facts can still be saved with a zero vector** (`record_human_fact`) if both embedding providers are down.
4. **Row-level security on the knowledge tables is not forced**, so it does not bind the owner role the app connects as (section 7.3); correctness there rests on explicit `workspace_id` predicates. Verifying and, if possible, connecting as a non-owner role is open.
5. **CORS defaults to `*`** when `ALLOWED_ORIGINS` is unset.
6. **The first request in each cold serverless instance runs the schema migration**, and there are two schema sources (`migrate()` and Alembic) that can drift.
7. **Roles come only from the token.** The `memberships.role` column is written at signup and invite-accept but not read for authorisation, so a user's effective role is whatever Clerk's `public_metadata` says.

---

## 15. File map

| Path | Lines | Role |
|---|---|---|
| `brain/engine.py` | 355 | `Brain`: the one public API — backfill, sync, ingest_event, search, decision_history, correct, record, harvest, ledger, usage |
| `brain/config.py` | ~320 | Environment, model routing, thresholds, prices, plans, boot check |
| `brain/auth.py` | 361 | Clerk and guest token verification, identity, role guards |
| `brain/core/ingest.py` | 131 | Filter, scrub, hash, dedupe, insert episodes |
| `brain/core/extract.py` | ~500 | Episode → facts, harvest, human facts, corrections, save |
| `brain/core/reconcile.py` | 166 | Candidate search, judge, apply |
| `brain/core/retrieve.py` | 635 | Hybrid search, fusion, filters, synthesis, guardrails, history |
| `brain/core/embed.py` | ~190 | Gemini embeddings, OpenRouter fallback, redaction |
| `brain/core/llm.py` | 393 | Every model call: routing, retry, breaker, fallback, metering, tracing |
| `brain/core/db_pg.py` | 1,584 | Connection pool, schema, RLS, all queries, key encryption |
| `brain/core/resilience.py` | 160 | Retry with jittered backoff; three-state circuit breaker |
| `brain/core/connectors/composio.py` | 517 | Connector: actions, normalisation, OAuth URLs, webhook triggers |
| `brain/core/log.py`, `metrics.py`, `sentry.py` | 145 / 213 / 128 | Structured logs, Prometheus, scrubbed error tracking |
| `brain/wrappers/api.py` | 2,920 | FastAPI server |
| `brain/wrappers/mcp_server.py`, `hooks/claude_code_hook.py`, `cli.py`, `slack_bot.py` | 354 / 205 / 211 / 604 | Other surfaces |
| `brain/wrappers/webapp/` | 2,945 + 1,356 | Single-page app and landing page |
| `brain/worker.py` | 415 | Optional arq worker (needs Redis) |
| `evals/` | — | `bench.py`, `corpus.yaml`, `aggregate.py`, `e2e.py` + `e2e_corpus.yaml` (end-to-end benchmark), `real_eval.py` (real-data validation), `thresholds.yaml`, `baseline.json`, `results/` |
| `scripts/` | — | `sync_once.py`, index and re-embed tools, RLS/index verification, seeding, smoke test |
| `migrations/` | 6 | Alembic revisions (0006 adds `reconcile_shadow_log`) |
| `.github/workflows/` | 3 | `ci.yml`, `e2e.yml`, `sync.yml` |

---

## 16. Environment variables

**Required in production:** `DATABASE_URL`, `CLERK_SECRET_KEY` (and `CLERK_PUBLISHABLE_KEY`), at least one model key, `BRAIN_WORKSPACE`, `BRAIN_KEY_ENCRYPTION_KEY`, `COMPOSIO_API_KEY` (for sync), `GEMINI_API_KEY` (embeddings).

**Models:** `BRAIN_EXTRACT_MODEL`, `BRAIN_SYNTH_MODEL`, `BRAIN_LLM_MODEL`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROK_API_KEY`, `MOONSHOT_API_KEY`, `BRAIN_EMBED_FALLBACK_MODEL`.

**Behaviour:** `BRAIN_ENV`, `BRAIN_DEV_BYPASS`, `BRAIN_RECONCILE_MODE` (`enforce` default, `shadow`, `off`), `BRAIN_RELATED_THRESHOLD` (0.70), `BRAIN_RECONCILE_K` (5), `BRAIN_RERANK` (off), `BRAIN_PG_MAXCONN`, `BRAIN_PG_CONNECT_TIMEOUT`, `BRAIN_SKIP_STARTUP_MIGRATE`, `BRAIN_ASYNC_WORKER`, `BRAIN_LLM_TIMEOUT` (30 s), `GUEST_TOKEN_SECRET`, `GUEST_SESSION_CAP`, `GUEST_DAILY_CAP`, `ALLOWED_ORIGINS`, `APP_BASE_URL`, `BRAIN_PLATFORM_OWNER`.

**Optional services:** `REDIS_URL`, `SENTRY_DSN`, `LANGFUSE_*`, `SLACK_*`, `COMPOSIO_WEBHOOK_SECRET`.
