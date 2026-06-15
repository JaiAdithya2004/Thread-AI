"""
FastAPI web server for the Company Brain web UI.
Run: python -m brain.wrappers.api   (or: brain-web)
Opens at http://127.0.0.1:7070
"""
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path
from brain.engine import Brain
from brain.core.connectors import REGISTRY
from brain.core import db as _db
from brain import config

app = FastAPI(title="Company Brain")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── request models ────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    query: str
    team_id: str | None = None
    cross_team: bool = False

class RecordRequest(BaseModel):
    text: str
    team_id: str | None = None
    shared: bool = False

class CorrectRequest(BaseModel):
    fact_id: str
    correction: str

class CreateTeamRequest(BaseModel):
    team_id: str
    name: str
    description: str = ""

class BackfillRequest(BaseModel):
    source: str
    team_id: str | None = None

# ── helpers ───────────────────────────────────────────────────────────────────
def _brain(team_id=None):
    return Brain(team_id=team_id)

def _enrich(facts, conn):
    """Attach episode source/date to each fact for display."""
    for f in facts:
        ep_id = f.get("episode_id")
        if ep_id:
            row = conn.execute(
                "SELECT source,title,url,author,occurred_at FROM episodes WHERE id=?",
                (ep_id,)).fetchone()
            if row:
                f.update({"ep_source": row["source"], "ep_title": row["title"],
                           "ep_url": row["url"], "ep_author": row["author"],
                           "ep_occurred_at": row["occurred_at"]})
    return facts

def _auto_discover_teams(b):
    """Auto-register any team_id found in facts that has no teams table row."""
    rows = b.conn.execute(
        "SELECT DISTINCT team_id FROM facts WHERE workspace_id=? AND team_id IS NOT NULL",
        (b.ws,)).fetchall()
    for row in rows:
        tid = row["team_id"]
        if tid and tid != "shared":
            _db.ensure_team(b.conn, b.ws, tid, tid.replace("-", " ").title())

# ── endpoints ─────────────────────────────────────────────────────────────────
@app.get("/api/status")
def status():
    """Returns LLM mode and which integrations have credentials set."""
    has_openai    = bool(os.getenv("OPENAI_API_KEY"))
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_gemini    = bool(os.getenv("GEMINI_API_KEY"))
    return {
        "mock_mode":  not (has_openai or has_anthropic or has_gemini),
        "llm_model":  config.LLM_MODEL,
        "keys": {
            "openai":    has_openai,
            "anthropic": has_anthropic,
            "gemini":    has_gemini,
            "slack":     bool(os.getenv("SLACK_BOT_TOKEN")),
            "google":    bool(os.getenv("GOOGLE_CREDENTIALS_JSON")),
            "notion":    bool(os.getenv("NOTION_TOKEN")),
        },
    }

@app.get("/api/teams")
def get_teams():
    b = _brain()
    _auto_discover_teams(b)   # surface any teams already in facts
    return b.list_teams()

@app.post("/api/teams")
def create_team(req: CreateTeamRequest):
    return _brain().create_team(req.team_id, req.name, req.description)

@app.post("/api/search")
def search(req: SearchRequest):
    b = _brain(req.team_id)
    result = b.search(req.query, surface="web", cross_team=req.cross_team)
    _enrich(result.get("facts", []), b.conn)
    return result

@app.get("/api/history")
def history(subject: str, team_id: str = None, cross_team: bool = False):
    b = _brain(team_id)
    result = b.decision_history(subject, cross_team=cross_team)
    _enrich(result.get("timeline", []), b.conn)
    return result

@app.post("/api/record")
def record(req: RecordRequest):
    visibility = "shared" if req.shared else "team"
    return _brain(req.team_id).record(req.text, visibility=visibility)

@app.post("/api/correct")
def correct(req: CorrectRequest):
    return _brain().correct(req.fact_id, req.correction)

@app.get("/api/ledger")
def ledger(team_id: str = None, limit: int = 50):
    return _brain(team_id).ledger(limit=limit)

@app.get("/api/facts")
def get_facts(team_id: str = None, limit: int = 40, cross_team: bool = False):
    b = _brain(team_id)
    conn, ws = b.conn, b.ws
    tid = team_id or b.team_id
    if cross_team or not team_id:
        rows = conn.execute(
            "SELECT f.*,e.source ep_source FROM facts f "
            "LEFT JOIN episodes e ON f.episode_id=e.id "
            "WHERE f.workspace_id=? AND f.invalidated_at IS NULL "
            "ORDER BY f.introduced_at DESC LIMIT ?", (ws, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT f.*,e.source ep_source FROM facts f "
            "LEFT JOIN episodes e ON f.episode_id=e.id "
            "WHERE f.workspace_id=? AND f.invalidated_at IS NULL "
            "AND (f.team_id=? OR f.visibility='shared') "
            "ORDER BY f.introduced_at DESC LIMIT ?", (ws, tid, limit)).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/connectors")
def connectors():
    """Real connection status based on env vars, not hardcoded."""
    has_google = bool(os.getenv("GOOGLE_CREDENTIALS_JSON"))
    live = {
        "sample":   True,
        "slack":    bool(os.getenv("SLACK_BOT_TOKEN")),
        "gmail":    has_google,
        "gdoc":     has_google,
        "calendar": has_google,
        "notion":   bool(os.getenv("NOTION_TOKEN")),
    }
    meta = {
        "sample":   {"name": "Demo Data",    "desc": "Offline sample data – no credentials needed", "icon": "🧪", "color": "#7c3aed"},
        "slack":    {"name": "Slack",         "desc": "Workspace channels and messages",             "icon": "💬", "color": "#4a154b"},
        "gmail":    {"name": "Gmail",         "desc": "Emails and threads",                          "icon": "📧", "color": "#ea4335"},
        "gdoc":     {"name": "Google Docs",   "desc": "Documents and specs",                         "icon": "📄", "color": "#1967d2"},
        "calendar": {"name": "Calendar",      "desc": "Events and meeting notes",                    "icon": "📅", "color": "#0f9d58"},
        "notion":   {"name": "Notion",        "desc": "Pages, wikis and databases",                  "icon": "📝", "color": "#555"},
    }
    return [
        {"id": k, "connected": live.get(k, False),
         **meta.get(k, {"name": k, "desc": "", "icon": "🔌", "color": "#6b7280"})}
        for k in REGISTRY
    ]

@app.post("/api/backfill")
def backfill(req: BackfillRequest):
    try:
        return _brain(req.team_id).backfill(req.source, team_id=req.team_id)
    except KeyError:
        raise HTTPException(400, f"Unknown source '{req.source}'")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/", response_class=HTMLResponse)
def index():
    p = Path(__file__).parent / "webapp" / "index.html"
    return HTMLResponse(p.read_text(encoding="utf-8"))

# ── entry point ───────────────────────────────────────────────────────────────
def main():
    import uvicorn
    uvicorn.run("brain.wrappers.api:app", host="127.0.0.1", port=7070, reload=True)

if __name__ == "__main__":
    main()
