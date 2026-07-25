# Company Brain

AI memory layer for your company. Connects to Slack and 1,000+ apps via Composio,
extracts facts, and answers questions with sources — over a web UI, MCP, or CLI.

---

## Quick Start

### 1. Install dependencies
```
pip install -e .
```

### 2. Set your API keys in `.env`

**Required — pick one LLM provider:**
| Provider | Where to get it | .env key |
|---|---|---|
| Gemini (free) | aistudio.google.com → Get API key | `GEMINI_API_KEY=` |
| OpenAI | platform.openai.com → API keys | `OPENAI_API_KEY=` |
| Anthropic | console.anthropic.com → API keys | `ANTHROPIC_API_KEY=` |

**Required for production database (Supabase — free):**
1. Create project at supabase.com
2. Settings → Database → copy URI → paste as `DATABASE_URL=`
3. Settings → API → copy JWT secret and anon key
4. Set `BRAIN_DB_BACKEND=postgres`

**Connectors:**
| What | .env key | Where to get it |
|---|---|---|
| Slack | `SLACK_BOT_TOKEN=` | Already set ✅ |
| All other apps (Gmail, Notion, GitHub, Jira…) | `COMPOSIO_API_KEY=` | app.composio.dev → Settings → API Keys |

### 3. Start the server

Double-click `start.bat`  
or from terminal:
```
start.bat
```

Opens at **http://localhost:7070**

---

## Connecting apps

All apps (Gmail, Google Docs, Calendar, Notion, GitHub, HubSpot, Jira, Linear, and 1,040+ more)
connect through **Composio** — one API key, one OAuth flow per app, no per-app credentials needed.

1. Set `COMPOSIO_API_KEY=` in `.env` and restart
2. Go to **Connections** tab in the web UI
3. Type any app name (e.g. `gmail`, `github`, `notion`) → click **+ Connect app**
4. Complete OAuth in the browser tab that opens
5. Click **↻ Sync** to pull data in

Slack connects directly (already working).

---

## Slash commands (in the chat box)

Type `/` to see the full command palette. Key commands:

| Command | What it does |
|---|---|
| `/sync slack` | Pull latest Slack messages |
| `/sync composio` | Pull from all Composio-connected apps |
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
    "company-brain": {
      "command": "python",
      "args": ["-m", "brain.wrappers.mcp_server"],
      "cwd": "C:/Users/jaiad/Desktop/Company Brain"
    }
  },
  "hooks": {
    "UserPromptSubmit": [{
      "matcher": "*",
      "hooks": [{"type": "command", "command": "python",
        "args": ["C:/Users/jaiad/Desktop/Company Brain/brain/wrappers/hooks/claude_code_hook.py"],
        "timeout": 15}]
    }],
    "Stop": [{
      "hooks": [{"type": "command", "command": "python",
        "args": ["C:/Users/jaiad/Desktop/Company Brain/brain/wrappers/hooks/claude_code_hook.py"],
        "timeout": 30}]
    }]
  }
}
```

Claude will automatically inject relevant company facts on every prompt and harvest new facts from responses.

---

## Project structure

```
brain/
  auth.py          — JWT auth + identity (Supabase or bypass)
  config.py        — env var loading + defaults
  engine.py        — Brain class: single public API surface
  core/
    db.py          — routes to SQLite or Postgres
    db_sqlite.py   — SQLite + sqlite-vec backend
    db_pg.py       — Postgres + pgvector backend
    extract.py     — LLM fact extraction
    retrieve.py    — semantic search + answer synthesis
    ingest.py      — episode dedup + worthiness filter
    embed.py       — fastembed (bge-small, local, no API)
    llm.py         — litellm wrapper (any provider)
    connectors/
      slack.py     — Slack connector (direct)
      composio.py  — Composio connector (1,000+ apps)
  wrappers/
    api.py         — FastAPI web server
    mcp_server.py  — MCP server for Claude Code
    cli.py         — CLI (brain search / backfill / log)
    hooks/
      claude_code_hook.py  — UserPromptSubmit + Stop hooks
    webapp/
      index.html   — Single-page web UI

evals/             — Eval harness (python -m evals.run)
scripts/           — migrate_sqlite_to_pg.py
```
