# Thread

**AI memory layer for software teams.** Thread connects to the tools your team already uses, extracts facts from conversations and documents, and answers questions with cited sources - over a web UI, an MCP server, or a REST API.

## What it does

- **Hybrid retrieval** - combines vector search (pgvector HNSW, 768-dim Gemini embeddings) with Postgres full-text search, fused via Reciprocal Rank Fusion
- **Automatic fact extraction** - ingests episodes from Slack, Gmail, Notion, Drive, and 1,000+ apps; extracts structured facts with deduplication
- **Source citations** - every answer includes the exact facts used and where they came from
- **MCP server** - plugs into Claude Code, Cursor, and any MCP-compatible agent; facts inject automatically on every prompt
- **Team scoping** - workspace and team-level isolation; facts are only visible to the right people
- **Circuit breakers + caching** - Redis response cache, LLM circuit breaker, graceful degradation when APIs are down

## Project structure

```
brain/                      # Core Python package
  config.py                 # All env var loading and defaults
  engine.py                 # Brain class - single public API surface
  auth.py                   # JWT + API key authentication
  worker.py                 # Background ingestion queue

  core/
    db.py                   # Re-exports db_pg (all DB access goes here)
    db_pg.py                # Postgres + pgvector backend
    embed.py                # Gemini embedding client (768-dim)
    extract.py              # Document parsing and fact extraction
    ingest.py               # Episode dedup + ingestion pipeline
    retrieve.py             # Hybrid search + RRF + answer synthesis
    llm.py                  # LiteLLM wrapper (any provider, metered)
    resilience.py           # Circuit breakers, retry decorators
    metrics.py              # Prometheus instrumentation
    connectors/
      slack.py              # Slack connector (direct)
      composio.py           # 1,000+ app connector via Composio

  wrappers/
    api.py                  # FastAPI REST server
    mcp_server.py           # MCP server for Claude Code / Cursor
    cli.py                  # CLI (brain search / backfill / log)
    slack_bot.py            # Slack bot (Bolt)
    hooks/
      claude_code_hook.py   # UserPromptSubmit + Stop lifecycle hooks
    webapp/
      index.html            # Single-page web UI
      landing.html          # Marketing landing page

api/                        # Vercel serverless entrypoint
migrations/                 # Alembic schema migrations (4 versions)
scripts/                    # Ops and benchmark utilities
tests/                      # pytest suite
evals/                      # Retrieval quality eval harness
docs/                       # Architecture and ops runbooks
```

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Web framework | FastAPI |
| Database | PostgreSQL + pgvector (Supabase or self-hosted) |
| Embeddings | Google Gemini (`gemini-embedding-001`, 768-dim) |
| Vector index | pgvector HNSW (`m=16, ef_construction=64`) |
| LLM routing | LiteLLM (Gemini / OpenAI / Anthropic) |
| Auth | Clerk (JWT + API keys) |
| Cache | Redis |
| Deployment | Vercel (API) + Supabase (DB) |
| Agent protocol | MCP (Model Context Protocol) |

## Quick start

### 1. Install dependencies

```bash
pip install -e .
```

### 2. Configure environment

```bash
cp .env.example .env
```

Required variables:

```env
# Database (Supabase or self-hosted Postgres)
DATABASE_URL=postgresql://user:pass@host:5432/db

# Embeddings (required)
GEMINI_API_KEY=your_key_here

# LLM for synthesis (pick one)
GEMINI_API_KEY=...
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
```

See `.env.example` for the full list including connectors, auth, and Redis.

### 3. Run migrations

```bash
alembic upgrade head
```

### 4. Start the server

```bash
python -m brain.wrappers.api
```

Opens at `http://localhost:7070`

### 5. Start the background worker (auto-sync)

```bash
arq brain.worker.WorkerSettings
```

This runs the polling loop that automatically syncs all connected apps every `BRAIN_POLL_INTERVAL_MINUTES` (default: 10 minutes). Requires Redis (`REDIS_URL` in `.env`).

The worker:
- Discovers all active workspaces
- For each workspace, checks when it was last synced per connector
- Fetches only new content since the last sync (watermark-based, not full re-fetch)
- Deduplicates before storing - re-running is always safe

## Connecting your AI agent (Claude Code / Cursor)

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "thread": {
      "command": "python",
      "args": ["-m", "brain.wrappers.mcp_server"],
      "cwd": "/absolute/path/to/thread",
      "env": {
        "DATABASE_URL": "postgresql://...",
        "BRAIN_TEAM": "engineering"
      }
    }
  },
  "hooks": {
    "UserPromptSubmit": [{
      "matcher": "*",
      "hooks": [{"type": "command", "command": "python",
        "args": ["/absolute/path/to/thread/brain/wrappers/hooks/claude_code_hook.py"],
        "timeout": 15}]
    }],
    "Stop": [{"hooks": [{"type": "command", "command": "python",
      "args": ["/absolute/path/to/thread/brain/wrappers/hooks/claude_code_hook.py"],
      "timeout": 30}]}]
  }
}
```

Every prompt gets relevant company facts injected before the model sees it. After the session ends, new decisions are extracted and stored automatically.

### MCP tools

| Tool | What it does |
|---|---|
| `search_company_facts` | Semantic + full-text search across all stored facts |
| `get_decision_history` | Ordered timeline of decisions on any topic |
| `record_fact` | Save a fact (decision, ownership, context) |
| `correct_fact` | Supersede an outdated fact with the correct version |

## Running tests

```bash
pytest
```

85+ tests covering auth, retrieval, extraction, privacy isolation, metering, and validation. Integration tests require `DATABASE_URL`; all other tests run without one.

## Eval harness

Measures retrieval quality against a benchmark dataset of 28 questions:

```bash
python -m evals.run
```

Current results: Hit@5 = 1.000, MRR = 0.961, NDCG@5 = 0.898

Capture a baseline and compare regressions:

```bash
python scripts/bench_snapshot.py    # save baseline
python scripts/bench_compare.py     # compare against it
```

## Scripts

| Script | Purpose |
|---|---|
| `scripts/smoke_test.py` | End-to-end smoke test against a live server |
| `scripts/seed_demo.py` | Seed a demo workspace with synthetic facts |
| `scripts/reembed_gemini.py` | Batch re-embed all facts after a model change |
| `scripts/rebuild_index.py` | Rebuild pgvector HNSW index after re-embedding |
| `scripts/verify_indexes.py` | Confirm DB indexes are present and healthy |
| `scripts/check_rls.py` | Validate Postgres row-level security enforcement |
| `scripts/explain_queries.py` | Run EXPLAIN ANALYZE on key queries |
