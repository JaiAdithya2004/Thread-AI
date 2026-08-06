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
