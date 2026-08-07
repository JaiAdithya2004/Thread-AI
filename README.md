# Intellark

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

## Using with Claude Code (MCP)

Add to `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "intellark": {
      "command": "python",
      "args": ["-m", "brain.wrappers.mcp_server"],
      "cwd": "/path/to/intellark"
    }
  },
  "hooks": {
    "UserPromptSubmit": [{
      "matcher": "*",
      "hooks": [{"type": "command", "command": "python",
        "args": ["/path/to/intellark/brain/wrappers/hooks/claude_code_hook.py"],
        "timeout": 15}]
    }],
    "Stop": [{
      "hooks": [{"type": "command", "command": "python",
        "args": ["/path/to/intellark/brain/wrappers/hooks/claude_code_hook.py"],
        "timeout": 30}]
    }]
  }
}
```

Claude will automatically inject relevant company facts on every prompt and harvest new facts from responses.

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
