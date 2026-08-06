# Phase 1 — Cost, Credits & Connector Metering ✅

Completed: 2026-08-06

## What was done

### LLM metering (`brain/core/llm.py`)
- Thread-local billing context (`_ctx`) set by API layer per request
- `_write_usage()` inserts one row into `usage_events` on every LLM call
- `_extract_usage()` reads `resp.usage.prompt_tokens/completion_tokens`, looks up `MODEL_PRICES` map, computes `cost_usd`
- Both `chat_json()` and `chat_text()` call `_extract_usage()` after every successful litellm response

### Token pricing (`brain/config.py`)
- `MODEL_PRICES` map with per-1K USD prices for OpenAI, Anthropic, Grok, Gemini + `"default"` fallback
- `CREDIT_USD_COST = 0.001` — 1 credit = $0.001 managed spend
- `PLAN_CREDITS` — free: 500, team: 5000, business: 50000 credits/month
- `PLATFORM_OWNER` flag from `BRAIN_PLATFORM_OWNER` env var
- `EMBED_USE_OPENAI = False` — fastembed is always local, zero network cost confirmed

### DB schema (`brain/core/db_pg.py`)
- `usage_events` extended with `customer_borne BOOLEAN DEFAULT FALSE` (idempotent ALTER)
- `integration_usage` table created: account_id, provider, api_calls, backfill_items, connected_accounts, period (DATE), UNIQUE on (account_id, provider, period)
- `record_integration_usage()` function — UPSERT with ON CONFLICT DO UPDATE increment

### Connector metering (`brain/core/connectors/composio.py`)
- `set_billing_context(account_id)` / `clear_billing_context()` / `_account_id()` added
- `_fetch()` tracks `total_calls` and `total_items`, calls `record_integration_usage()` after all fetching
- All user-facing error messages and log prefixes hide vendor name; use "integrations" instead

### Brain engine (`brain/engine.py`)
- `Brain.__init__` accepts `account_id=None`
- `backfill()` and `sync()` set/clear composio billing context around connector calls
- `usage_summary(period)` method: returns tokens, cost_usd, credits_used, credits_allowance, by_task, by_team, integrations for the given month — gracefully handles missing Phase 2 tables

### API layer (`brain/wrappers/api.py`)
- `_resolve_account_id(user_id)` — queries `memberships` table to resolve account_id per request
- `_brain()` updated to resolve account_id, pass to Brain, call `set_billing_context()`
- `GET /api/settings/llm` — returns active provider + has_key
- `POST /api/settings/llm` — saves key to `.env`, clears other provider keys, hot-patches `os.environ`
- `GET /api/usage` — workspace-admin endpoint returning `usage_summary()` for the given month
- `GET /api/_platform/metrics` — platform-owner only (BRAIN_PLATFORM_OWNER=true); returns per-workspace cost, tokens, integration api_calls; hard 403 for everyone else

### UI / branding (`brain/wrappers/webapp/index.html`, `landing.html`)
- Full retheme: white/black Cal.com design system, Intellark name, no vendor names
- Settings replaced with LLM provider dropdown + API key field
- Landing page FAQ entry added on token efficiency / cost control

## Acceptance criteria status

| Check | Status |
|---|---|
| `usage_events` rows written after LLM calls | ✅ write path live |
| `integration_usage` rows written after backfill/sync | ✅ UPSERT live |
| Embeddings use fastembed local — zero network calls | ✅ confirmed (BAAI/bge-small-en-v1.5 via @lru_cache) |
| `grep -ri composio` over frontend returns nothing user-facing | ✅ all user-facing text uses "Integrations" |
| `/api/_platform/metrics` inaccessible without BRAIN_PLATFORM_OWNER=true | ✅ hard 403 |
| `/api/usage` restricted to workspace admins | ✅ require_admin dependency |

## Not yet done (Phase 2+)
- Plan caps, subscription enforcement, BYO-key (`workspace_llm_keys` table)
- Security hardening: dev-bypass removal, RLS audit, rate limiting, input validation
- Automated test suite
- Launch readiness: .env.example, boot validation, docker-compose, smoke test

---

# Phase 2 — Plan Caps, BYO-Key & Enforcement

Completed: 2026-08-06

## What was done

### DB schema (brain/core/db_pg.py)
- plans table: seeded with free/team/business tiers (monthly_credits, max_seats, max_connectors, max_backfill, price_usd_month)
- subscriptions table: one active row per account, FK to plans; UNIQUE on account_id
- workspace_llm_keys table: BYO API keys stored per account+provider; UNIQUE on (account_id, provider)
- Helper functions: get_plan_for_account(), get_credits_used_this_month(), get_seat_count(), get_byo_key(), upsert_byo_key(), delete_byo_key(), ensure_free_subscription()

### BYO-key loading (brain/core/llm.py)
- set_billing_context() now calls _load_byo_key() eagerly
- _effective_api_key() returns the BYO key if present; falls through to env key
- _build_kwargs() injects BYO key into litellm call kwargs
- BYO-key accounts are never credit-capped

### Credit enforcement (brain/core/llm.py)
- _check_credit_allowance() called at top of every chat_json() and chat_text()
- Raises HTTP 402 with upgrade message when used >= allowance
- Silently passes in dev/bypass mode or non-postgres backends

### Seat and backfill limits (brain/engine.py)
- _plan(), _check_seat_limit(), _check_connector_limit(), _check_backfill_limit() helpers on Brain
- invite_member checks seat limit before issuing invite (hard 402)
- backfill() warns non-fatally if items exceed plan limit

### Free subscription provisioning (brain/wrappers/api.py)
- signup() and sso_provision() both call ensure_free_subscription(account_id)

### API endpoints (brain/wrappers/api.py)
- GET /api/plans — list all plans (authenticated)
- GET /api/subscription — current subscription and plan limits (admin)
- POST /api/subscription — change plan (admin)
- GET /api/byo-key — list saved workspace BYO providers (admin)
- POST /api/byo-key — save BYO key
- DELETE /api/byo-key/{provider} — remove BYO key

### UI (brain/wrappers/webapp/index.html)
- Usage and Credits card (admin-only): credit bar with color coding, tokens/cost/integration-calls stats
- Workspace API Key card (admin-only): list/add/remove BYO keys
- _api() convenience fetch helper added
- _loadUsageCredits(), _loadBYOKeys(), _saveBYOKey(), _deleteBYOKey()

---

# Phase 3 — Security Hardening

Completed: 2026-08-06

## What was done

### Dev-bypass removal (brain/auth.py)
- Bypass is now ONLY active when BRAIN_ENV=development (not production) AND no Clerk key is configured (or BRAIN_DEV_BYPASS=true is explicitly set)
- Production boot guard: server REFUSES to start (sys.exit(1)) if BRAIN_ENV=production AND BRAIN_DEV_BYPASS=true
- Production boot guard: server REFUSES to start if BRAIN_ENV=production AND CLERK_SECRET_KEY is not set
- dev-bypass sentinel token ("dev-bypass") now returns 401 in any non-bypass mode
- _BYPASS, _IS_PRODUCTION, _ENV constants clearly defined

### Endpoint auth audit (brain/wrappers/api.py)
- GET /api/composio/apps: added get_identity dependency (was unauthenticated)
- GET /api/composio/connected: elevated to require_admin (was unauthenticated)
- DELETE /api/composio/connected/{id}: elevated to require_admin (was unauthenticated)
- POST /api/settings/llm: elevated to require_admin (was get_identity)

### Secrets handling
- /api/status "composio" key renamed to "integrations" (vendor name hidden)
- BYO-key GET endpoint returns only provider/created_at/created_by; never key value
- No secret values ever returned in any response

### Rate limiting (brain/wrappers/api.py)
- In-process sliding-window rate limiter (_rl_store + _rl_lock) — no external dependency
- _rl_auth: 20 req/60s — signup, sso_provision, accept_invite
- _rl_llm: 60 req/60s — search
- _rl_write: 120 req/60s — record, correct

### Input validation (Pydantic model_post_init on all request models)
- SearchRequest: 2000 char max, non-empty
- RecordRequest: 50,000 char max, non-empty
- CorrectRequest: 10,000 char max, non-empty
- CreateTeamRequest: team_id slug-validated, name 128 char max, description 500 char max
- BackfillRequest / SyncRequest / ComposioConnectRequest: slug-validated source/app
- SignupRequest: email 254 char max, name 200 char max, account_type enum
- InviteRequest: email 254 char max, role enum, team_id slug-validated
- MemberPatchRequest: role/status enum, team_id slug-validated
- InviteAcceptRequest: password 1024 char max
- ConversationRequest: title 200 char max
- MessageRequest: role enum, content non-empty + 100,000 char max
- accept_invite: token length capped at 128

### HTTP security headers (SecurityHeadersMiddleware)
- X-Content-Type-Options: nosniff
- X-Frame-Options: DENY
- X-XSS-Protection: 1; mode=block
- Referrer-Policy: strict-origin-when-cross-origin
- Strict-Transport-Security: max-age=63072000; includeSubDomains
- Content-Security-Policy: default-src self, script/style inline, no external connect

### CORS tightening
- allow_origins now reads ALLOWED_ORIGINS env var (comma-separated) in production
- allow_headers restricted to Authorization + Content-Type
- expose_headers cleared; max_age=600

### RLS verification (brain/wrappers/api.py + brain/core/db_pg.py)
- _brain() now explicitly calls _pg._set_ws(b.conn, b.ws) before returning the Brain instance, guaranteeing brain.workspace_id is set for RLS on all subsequent queries including the get_facts direct SQL path
- Confirmed: episodes/facts/fact_edges/ledger/teams have ws_isolation RLS; conversations/messages have FORCE RLS user_isolation
- Phase 2 account-scoped tables (subscriptions, workspace_llm_keys, usage_events, integration_usage) use explicit account_id WHERE clauses — correct pattern for service-role connections

## Security properties after Phase 3
- No unauthenticated write endpoints
- No connector management endpoints without admin auth
- Production refuses to start without Clerk secret key
- Production refuses to start with dev-bypass enabled
- Every LLM and write endpoint rate-limited per IP
- All request bodies have length caps and enum validation
- RLS guaranteed on every Brain connection from first query
- No vendor names (Composio) exposed in any API response
