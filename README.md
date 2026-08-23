# Thread

AI memory layer for your company. Connects to Slack and 1,000+ apps, extracts facts, and answers questions with sources — over a web UI, MCP, or CLI.

---

## Quick Start

### 1. Install dependencies
```
pip install -e .
```

### 2. Configure `.env`

Copy `.env.example` to `.env` and fill in your values.

**Required — pick one LLM provider:**

| Provider | Where to get it | `.env` key |
|---|---|---|
| Gemini (free) | [aistudio.google.com](https://aistudio.google.com) → Get API key | `GEMINI_API_KEY=` |
| OpenAI | [platform.openai.com](https://platform.openai.com) → API keys | `OPENAI_API_KEY=` |
| Anthropic | [console.anthropic.com](https://console.anthropic.com) → API keys | `ANTHROPIC_API_KEY=` |
| xAI Grok | [console.x.ai](https://console.x.ai) → API keys | `GROK_API_KEY=` |

**Required for production database (Supabase):**
1. Create project at [supabase.com](https://supabase.com)
2. Settings → Database → URI (use the pooler URI, port 6543) → paste as `DATABASE_URL=`
3. Set `BRAIN_DB_BACKEND=postgres`

**Authentication (multi-user):**
1. Create app at [dashboard.clerk.com](https://dashboard.clerk.com)
2. API Keys → copy `CLERK_PUBLISHABLE_KEY` and `CLERK_SECRET_KEY`
3. Set `BRAIN_ENV=production` when deploying

**Connectors:**

| What | `.env` key | Where to get it |
|---|---|---|
| Slack | `SLACK_BOT_TOKEN=` | [api.slack.com](https://api.slack.com) → Your Apps |
| 1,000+ apps (Gmail, Notion, GitHub…) | `COMPOSIO_API_KEY=` | [app.composio.dev](https://app.composio.dev) → Settings → API Keys |

### 3. Start the server

```
python -m brain.wrappers.api
```

Or on Windows, double-click `start.bat`.

Opens at **http://localhost:7070**


## Connecting apps

Go to the **Connections** tab in the web UI:
1. Type any app name (e.g. `gmail`, `github`, `notion`) → click **+ Connect app**
2. Complete OAuth in the browser tab that opens
3. Click **↻ Sync** to pull data in

Slack connects directly with `SLACK_BOT_TOKEN`.

---

## Slash commands (in the chat box)

| Command | What it does |
|---|---|
| `/sync slack` | Pull latest Slack messages |
| `/sync composio` | Pull from all connected apps |
| `/backfill slack` | Pull full Slack history (run once) |
| `/record <text>` | Save a fact directly |
| `/history <topic>` | See decision history for a topic |
| `/facts` | Show recent facts in memory |
| `/status` | Show which keys and connections are active |

---

## Connecting your AI agent (Claude Code / Cursor)

This is the primary experience. Once wired up:
- **Every prompt** automatically gets relevant company facts injected as context (logged to the ledger with `surface="hook-inject"`).
- **Every response** is harvested for new facts — stored with `source_kind="agent"`, deduped, ledger-logged.
- **MCP tools** let the agent explicitly search, record, correct, and trace decision history.

### Step 1 — MCP server

Add to `~/.claude/settings.json` (Cursor: `.cursor/mcp.json`):

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
  }
}
```

### Step 2 — Lifecycle hooks (auto-inject + harvest)

Add to the same `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "matcher": "*",
      "hooks": [{"type": "command", "command": "python",
        "args": ["/absolute/path/to/thread/brain/wrappers/hooks/claude_code_hook.py"],
        "timeout": 15}]
    }],
    "Stop": [{
      "hooks": [{"type": "command", "command": "python",
        "args": ["/absolute/path/to/thread/brain/wrappers/hooks/claude_code_hook.py"],
        "timeout": 30}]
    }]
  }
}
```

### MCP tools available to your agent

| Tool | What it does |
|---|---|
| `search_company_facts` | Semantic + FTS search — use before answering company questions |
| `get_decision_history` | Ordered timeline of decisions about any topic |
| `record_fact` | Store a human-authored fact (decision, commitment, ownership) |
| `correct_fact` | Supersede a wrong fact with the correct version |
| `harvest_from_response` | Explicit harvest trigger for a specific response |
| `list_teams` | List all registered teams |

### How to verify it's working

1. Ask Claude Code: *"What database are we using?"* — it should call `search_company_facts`.
2. State a decision: *"We decided to use Postgres for billing."* — after the session ends, check the ledger: the fact should appear with `source_kind="agent"`.
3. Run `/status` in the web UI to see the ledger with `surface="hook-inject"` entries.

---

## Running tests

```bash
pytest
```

85 tests — covers auth, metering, validation, and privacy isolation. Integration tests (ingest/retrieve pipeline) require a `DATABASE_URL`; all other tests run without one.

---

## Smoke test (against a live server)

```bash
python scripts/smoke_test.py --url http://localhost:7070
```

Checks status endpoint, security headers, search responses, and platform gate.

---

## Project structure

```
brain/
  auth.py          — JWT auth + identity (Clerk or bypass)
  config.py        — env var loading, defaults, boot validation
  engine.py        — Brain class: single public API surface
  core/
    db.py          — re-exports db_pg; all DB access goes here
    db_pg.py       — Postgres + pgvector backend (Supabase or self-hosted)
    extract.py     — LLM fact extraction
    retrieve.py    — semantic search + answer synthesis
    ingest.py      — episode dedup + worthiness filter
    embed.py       — fastembed (bge-small, local, zero API cost)
    llm.py         — litellm wrapper (any provider, metered)
    connectors/
      slack.py     — Slack connector (direct)
      composio.py  — Integration connector (1,000+ apps)
  wrappers/
    api.py         — FastAPI web server
    mcp_server.py  — MCP server for Claude Code
    cli.py         — CLI (brain search / backfill / log)
    hooks/
      claude_code_hook.py  — UserPromptSubmit + Stop hooks
    webapp/
      index.html   — Single-page web UI

scripts/
  smoke_test.py          — Live server smoke test

tests/                   — pytest suite (85 tests)
```
