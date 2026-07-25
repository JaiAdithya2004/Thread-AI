"""
FastAPI web server for the Company Brain web UI.
Run: python -m brain.wrappers.api   (or: brain-web)
Opens at http://127.0.0.1:7070

Authentication
--------------
Every API endpoint (except / and /api/status) requires an Authorization: Bearer <token>
header containing a Supabase JWT.  Identity (workspace, team, principals) is derived
exclusively from the verified token — never from query parameters or request bodies.

Development / single-tenant mode: if SUPABASE_JWT_SECRET is not set, auth runs in
BYPASS mode and all requests use the config defaults.  See brain/auth.py.
"""
import os
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path

from brain.auth import Identity, get_identity, require_admin
from brain.engine import Brain
from brain.core.connectors import REGISTRY
from brain.core import db as _db
from brain import config

app = FastAPI(title="Company Brain")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── request models ────────────────────────────────────────────────────────────
# NOTE: team_id is intentionally absent from all write-request bodies.
#       The caller's team comes from the verified JWT, not from the client.

class SearchRequest(BaseModel):
    query: str
    cross_team: bool = False          # privileged: include other teams' shared facts

class RecordRequest(BaseModel):
    text: str
    shared: bool = False              # true = mark as company-wide (visibility="shared")

class CorrectRequest(BaseModel):
    fact_id: str
    correction: str

class CreateTeamRequest(BaseModel):
    team_id: str
    name: str
    description: str = ""

class BackfillRequest(BaseModel):
    source: str

class SyncRequest(BaseModel):
    source: str

class ComposioConnectRequest(BaseModel):
    app: str                        # composio app slug e.g. "gmail", "slack", "github"
    redirect_url: str = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _brain(identity: Identity, team_id: str = None) -> Brain:
    """Build a Brain scoped to the authenticated identity."""
    return Brain(
        workspace_id=identity.workspace_id,
        team_id=team_id or identity.team_id,
        caller=identity.user_id,
        principals=identity.principals,
    )


def _q(conn, sql_sqlite, sql_pg, params):
    """Run the right SQL variant based on active backend."""
    from brain.core.db import BACKEND
    sql = sql_pg if BACKEND == "postgres" else sql_sqlite
    if BACKEND == "postgres":
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows
    return conn.execute(sql, params).fetchall()


def _q1(conn, sql_sqlite, sql_pg, params):
    rows = _q(conn, sql_sqlite, sql_pg, params)
    return rows[0] if rows else None


def _enrich(facts, conn):
    """Attach episode source/date to each fact for display."""
    for f in facts:
        ep_id = f.get("episode_id")
        if ep_id:
            row = _q1(conn,
                "SELECT source,title,url,author,occurred_at FROM episodes WHERE id=?",
                "SELECT source,title,url,author,occurred_at FROM episodes WHERE id=%s",
                (ep_id,))
            if row:
                f.update({"ep_source": row["source"], "ep_title": row["title"],
                           "ep_url": row["url"], "ep_author": row["author"],
                           "ep_occurred_at": row["occurred_at"]})
    return facts


def _auto_discover_teams(b: Brain):
    """Auto-register any team_id found in facts that has no teams table row."""
    rows = _q(b.conn,
        "SELECT DISTINCT team_id FROM facts WHERE workspace_id=? AND team_id IS NOT NULL",
        "SELECT DISTINCT team_id FROM facts WHERE workspace_id=%s AND team_id IS NOT NULL",
        (b.ws,))
    for row in rows:
        tid = row["team_id"]
        if tid and tid != "shared":
            _db.ensure_team(b.conn, b.ws, tid, tid.replace("-", " ").title())


# ── public endpoints (no auth required) ──────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    p = Path(__file__).parent / "webapp" / "index.html"
    return HTMLResponse(p.read_text(encoding="utf-8"))


@app.get("/api/status")
def status():
    """Returns LLM mode, auth mode, and which integrations have credentials set."""
    from dotenv import dotenv_values
    env_file = Path(__file__).parent.parent.parent / ".env"
    live = dotenv_values(env_file) if env_file.exists() else {}

    def _get(key):
        return bool(live.get(key) or os.getenv(key))

    from brain.auth import _BYPASS
    return {
        "mock_mode":  not (_get("OPENAI_API_KEY") or _get("ANTHROPIC_API_KEY")
                           or _get("GEMINI_API_KEY") or _get("GROK_API_KEY")),
        "llm_model":  config.LLM_MODEL,
        "auth_mode":  "bypass" if _BYPASS else "supabase",
        "keys": {
            "openai":    _get("OPENAI_API_KEY"),
            "anthropic": _get("ANTHROPIC_API_KEY"),
            "gemini":    _get("GEMINI_API_KEY"),
            "grok":      _get("GROK_API_KEY"),
            "slack":     _get("SLACK_BOT_TOKEN"),
            "google":    _get("GOOGLE_CREDENTIALS_JSON"),
            "notion":    _get("NOTION_TOKEN"),
            "supabase":  _get("SUPABASE_JWT_SECRET"),
        },
    }


# ── auth-gated endpoints ──────────────────────────────────────────────────────

@app.get("/api/me")
def me(identity: Identity = Depends(get_identity)):
    """Return the caller's resolved identity (debug / UI persona display)."""
    return {
        "user_id":      identity.user_id,
        "email":        identity.email,
        "workspace_id": identity.workspace_id,
        "team_id":      identity.team_id,
        "role":         identity.role,
        "is_admin":     identity.is_admin,
        "principals":   identity.principals,
    }


@app.get("/api/teams")
def get_teams(identity: Identity = Depends(get_identity)):
    b = _brain(identity)
    _auto_discover_teams(b)
    return b.list_teams()


@app.post("/api/teams")
def create_team(
    req: CreateTeamRequest,
    identity: Identity = Depends(require_admin),   # only admins can create teams
):
    return _brain(identity).create_team(req.team_id, req.name, req.description)


@app.post("/api/search")
def search(
    req: SearchRequest,
    identity: Identity = Depends(get_identity),
):
    # cross_team is honoured only if the caller has admin role
    cross = req.cross_team and identity.is_admin
    b = _brain(identity)
    result = b.search(req.query, surface="web", cross_team=cross)
    _enrich(result.get("facts", []), b.conn)
    return result


@app.get("/api/history")
def history(
    subject: str,
    cross_team: bool = False,
    identity: Identity = Depends(get_identity),
):
    cross = cross_team and identity.is_admin
    b = _brain(identity)
    result = b.decision_history(subject, cross_team=cross)
    _enrich(result.get("timeline", []), b.conn)
    return result


@app.post("/api/record")
def record(
    req: RecordRequest,
    identity: Identity = Depends(get_identity),
):
    visibility = "shared" if req.shared else "team"
    return _brain(identity).record(req.text, visibility=visibility)


@app.post("/api/correct")
def correct(
    req: CorrectRequest,
    identity: Identity = Depends(get_identity),
):
    return _brain(identity).correct(req.fact_id, req.correction)


@app.get("/api/ledger")
def ledger(
    limit: int = 50,
    identity: Identity = Depends(get_identity),
):
    # Admins see full ledger; regular members see only their team's
    b = _brain(identity)
    team_scope = None if identity.is_admin else identity.team_id
    return _db.read_ledger(b.conn, b.ws, limit, team_id=team_scope)


@app.get("/api/facts")
def get_facts(
    cross_team: bool = False,
    limit: int = 40,
    identity: Identity = Depends(get_identity),
):
    b = _brain(identity)
    conn, ws = b.conn, b.ws
    tid = identity.team_id
    cross = cross_team and identity.is_admin

    if cross:
        rows = _q(conn,
            "SELECT f.*,e.source ep_source FROM facts f "
            "LEFT JOIN episodes e ON f.episode_id=e.id "
            "WHERE f.workspace_id=? AND f.invalidated_at IS NULL "
            "ORDER BY f.introduced_at DESC LIMIT ?",
            "SELECT f.*,e.source ep_source FROM facts f "
            "LEFT JOIN episodes e ON f.episode_id=e.id "
            "WHERE f.workspace_id=%s AND f.invalidated_at IS NULL "
            "ORDER BY f.introduced_at DESC LIMIT %s",
            (ws, limit))
    else:
        rows = _q(conn,
            "SELECT f.*,e.source ep_source FROM facts f "
            "LEFT JOIN episodes e ON f.episode_id=e.id "
            "WHERE f.workspace_id=? AND f.invalidated_at IS NULL "
            "AND (f.team_id=? OR f.visibility='shared') "
            "ORDER BY f.introduced_at DESC LIMIT ?",
            "SELECT f.*,e.source ep_source FROM facts f "
            "LEFT JOIN episodes e ON f.episode_id=e.id "
            "WHERE f.workspace_id=%s AND f.invalidated_at IS NULL "
            "AND (f.team_id=%s OR f.visibility='shared') "
            "ORDER BY f.introduced_at DESC LIMIT %s",
            (ws, tid, limit))
    return [dict(r) for r in rows]


@app.get("/api/composio/apps")
def composio_apps(_identity: Identity = Depends(get_identity)):
    """List all 1,047+ Composio apps available to connect."""
    try:
        from brain.core.connectors.composio import list_all_apps
        return list_all_apps()
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/composio/connected")
def composio_connected(_identity: Identity = Depends(get_identity)):
    """List apps the user has already connected in their Composio account."""
    try:
        from brain.core.connectors.composio import list_connected_apps
        return list_connected_apps()
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/composio/connect")
def composio_connect(
    req: ComposioConnectRequest,
    _identity: Identity = Depends(get_identity),
):
    """Initiate OAuth for a Composio app. Returns a URL the user must open."""
    try:
        from brain.core.connectors.composio import get_connect_url
        url = get_connect_url(req.app, redirect_url=req.redirect_url or None)
        return {"app": req.app, "connect_url": url}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/connectors")
def connectors(_identity: Identity = Depends(get_identity)):
    """Real connection status based on env vars."""
    live = {
        "slack":    bool(os.getenv("SLACK_BOT_TOKEN")),
        "composio": bool(os.getenv("COMPOSIO_API_KEY")),
    }
    meta = {
        "slack": {
            "name": "Slack",
            "desc": "Workspace channels and messages",
            "icon": "slack",
            "env_var": "SLACK_BOT_TOKEN",
            "setup": "Create a Slack app → Bot Token Scopes: channels:history, channels:read, groups:history, groups:read → Install to workspace",
        },
        "composio": {
            "name": "Composio (1,000+ apps)",
            "desc": "Gmail, Notion, GitHub, HubSpot, Jira, Linear, Google Docs, Calendar, and 1,000+ more — one API key",
            "icon": "composio",
            "env_var": "COMPOSIO_API_KEY",
            "setup": "Sign up at app.composio.dev → Settings → API Keys → copy key → add COMPOSIO_API_KEY to .env → use the Connections tab to connect individual apps via OAuth",
        },
    }
    return [
        {
            "id": k,
            "connected": live.get(k, False),
            **meta.get(k, {"name": k, "desc": "", "icon": "default", "env_var": "", "setup": ""}),
        }
        for k in REGISTRY
    ]


@app.post("/api/backfill")
def backfill(
    req: BackfillRequest,
    identity: Identity = Depends(require_admin),
):
    try:
        return _brain(identity).backfill(req.source, team_id=identity.team_id)
    except KeyError:
        raise HTTPException(400, f"Unknown source '{req.source}'")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/sync")
def sync(
    req: SyncRequest,
    identity: Identity = Depends(get_identity),
):
    """Pull recent items (poll window) from a connector — lighter than backfill."""
    try:
        return _brain(identity).sync(req.source, team_id=identity.team_id)
    except KeyError:
        raise HTTPException(400, f"Unknown source '{req.source}'")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    import uvicorn
    uvicorn.run("brain.wrappers.api:app", host="127.0.0.1", port=7070, reload=False)


if __name__ == "__main__":
    main()
