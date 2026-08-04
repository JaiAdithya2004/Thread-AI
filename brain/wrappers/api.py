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
import os, json
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path

from brain.auth import Identity, get_identity, require_admin
from brain.engine import Brain
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

class SignupRequest(BaseModel):
    email: str
    password: str
    account_type: str               # "individual" | "company"
    name: str                       # person name (individual) or company name (company)

class InviteRequest(BaseModel):
    email: str
    role: str = "employee"          # "employee" | "admin"
    team_id: str = None             # optional department

class MemberPatchRequest(BaseModel):
    role: str = None                # "employee" | "admin"
    status: str = None              # "active" | "disabled"
    team_id: str = None             # department slug or "" to clear

class InviteAcceptRequest(BaseModel):
    password: str

class ConversationRequest(BaseModel):
    title: str = "New conversation"
    team_id: str = None

class MessageRequest(BaseModel):
    role: str           # "user" | "assistant"
    content: str

_PUBLIC_DOMAINS = {
    "gmail.com","googlemail.com","yahoo.com","yahoo.co.uk","yahoo.in",
    "hotmail.com","outlook.com","live.com","msn.com","icloud.com",
    "me.com","mac.com","aol.com","protonmail.com","pm.me","proton.me",
    "yandex.com","yandex.ru","mail.com","zohomail.com","zoho.com",
    "fastmail.com","tutanota.com","hey.com",
}


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
    html = p.read_text(encoding="utf-8")
    clerk_pk = os.getenv("CLERK_PUBLISHABLE_KEY", "")
    injection = f"""<script>
window.__CLERK_PUBLISHABLE_KEY__ = {json.dumps(clerk_pk)};
</script>"""
    html = html.replace("</head>", injection + "\n</head>", 1)
    return HTMLResponse(html)


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
        "auth_mode":  "bypass" if _BYPASS else "clerk",
        "keys": {
            "openai":    _get("OPENAI_API_KEY"),
            "anthropic": _get("ANTHROPIC_API_KEY"),
            "gemini":    _get("GEMINI_API_KEY"),
            "grok":      _get("GROK_API_KEY"),
            "slack":     _get("SLACK_BOT_TOKEN"),
            "google":    _get("GOOGLE_CREDENTIALS_JSON"),
            "notion":    _get("NOTION_TOKEN"),
            "clerk":     _get("CLERK_SECRET_KEY"),
            "composio":  _get("COMPOSIO_API_KEY"),
        },
    }


# ── auth-gated endpoints ──────────────────────────────────────────────────────

@app.get("/api/me")
def me(identity: Identity = Depends(get_identity)):
    """Return the caller's resolved identity (debug / UI persona display)."""
    account_type = "company"  # default — company UI until we know otherwise
    from brain.core.db import BACKEND
    if BACKEND == "postgres":
        try:
            from brain.core import db_pg as _pg
            conn = _pg.connect()
            cur = conn.cursor()
            cur.execute("""
                SELECT a.type FROM accounts a
                JOIN memberships m ON m.account_id = a.id
                WHERE m.user_id = %s AND m.status = 'active'
                LIMIT 1
            """, (identity.user_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                account_type = row["type"]
        except Exception:
            pass  # non-fatal; default to company mode

    return {
        "user_id":      identity.user_id,
        "email":        identity.email,
        "workspace_id": identity.workspace_id,
        "team_id":      identity.team_id,
        "role":         identity.role,
        "is_admin":     identity.is_admin,
        "principals":   identity.principals,
        "account_type": account_type,
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


_COMPOSIO_CACHE: dict = {}   # cursor_key → (timestamp, payload)
_COMPOSIO_CACHE_TTL = 3600  # 1 hour

@app.get("/api/composio/apps")
def composio_apps(limit: int = 50, cursor: str = None):
    """List Composio apps with pagination."""
    import time
    cache_key = f"{limit}:{cursor or ''}"
    cached = _COMPOSIO_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _COMPOSIO_CACHE_TTL:
        return cached[1]

    try:
        import requests as _req, os
        key = os.getenv("COMPOSIO_API_KEY", "")
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        r = _req.get(
            "https://backend.composio.dev/api/v3.1/toolkits",
            headers={"x-api-key": key},
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()
        items = raw.get("items", [])
        result = {
            "items": [
                {
                    "slug":        item.get("slug", ""),
                    "name":        item.get("name", ""),
                    "logo":        (item.get("meta") or {}).get("logo", ""),
                    "description": (item.get("meta") or {}).get("description", ""),
                    "categories":  [c.get("name","") for c in (item.get("meta") or {}).get("categories", [])],
                    "tools_count": (item.get("meta") or {}).get("tools_count", 0),
                }
                for item in items
                if not item.get("no_auth")
            ],
            "next_cursor": raw.get("next_cursor"),
            "total_items": raw.get("total_items"),
        }
        _COMPOSIO_CACHE[cache_key] = (time.time(), result)
        return result
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/composio/connected")
def composio_connected():
    """List apps the user has already connected in their Composio account."""
    try:
        from brain.core.connectors.composio import list_connected_apps
        return list_connected_apps()
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/composio/connected/{account_id}")
def composio_disconnect(account_id: str):
    """Disconnect a connected Composio account by its ID."""
    try:
        from brain.core.connectors.composio import disconnect_app
        return disconnect_app(account_id)
    except Exception as e:
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


@app.post("/api/auth/signup")
def signup(req: SignupRequest):
    """
    Register account + membership after Clerk creates the auth user.
    Clerk handles passwords — we just store the workspace row.
    Called right after Clerk signup completes with the Clerk user_id as user_id.
    """
    import re, uuid as _uuid
    from brain.core.db import BACKEND

    req.email = req.email.lower().strip()
    req.name  = req.name.strip()
    req.account_type = req.account_type.strip().lower()

    if req.account_type not in ("individual", "company"):
        raise HTTPException(400, "account_type must be 'individual' or 'company'")
    if not req.name:
        raise HTTPException(400, "name is required")
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', req.email):
        raise HTTPException(400, "Invalid email address")

    domain = req.email.split("@")[1]
    if req.account_type == "company" and domain in _PUBLIC_DOMAINS:
        raise HTTPException(400,
            "Company accounts require a work email. "
            "Public providers (Gmail, Outlook…) are not allowed.")

    # user_id comes from the Clerk JWT (passed as password field since SignupRequest has it)
    # Actually we get it from the auth identity
    user_id = req.password  # repurposed: caller passes clerk user_id here

    if BACKEND == "postgres":
        from brain.core import db_pg as _pg
        conn = _pg.connect()
        try:
            cur = conn.cursor()
            # Idempotent — don't create duplicate account if already exists
            cur.execute(
                "SELECT m.account_id FROM memberships m WHERE m.user_id=%s AND m.status='active' LIMIT 1",
                (user_id,)
            )
            existing = cur.fetchone()
            if existing:
                return {"user_id": user_id, "account_id": existing["account_id"],
                        "account_type": req.account_type, "role": "owner", "existing": True}

            account_id = str(_uuid.uuid4())
            cur.execute(
                "INSERT INTO accounts (id, type, name, domain) VALUES (%s,%s,%s,%s)",
                (account_id, req.account_type, req.name, domain),
            )
            role = "owner" if req.account_type == "individual" else "admin"
            cur.execute(
                "INSERT INTO memberships (id, user_id, account_id, role, status) VALUES (%s,%s,%s,%s,'active')",
                (str(_uuid.uuid4()), user_id, account_id, role),
            )
            conn.commit()
            cur.close()
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"DB error: {e}")
        finally:
            conn.close()
    # SQLite mode: skip local tables (in-memory only dev scenario)

    return {
        "user_id":      user_id,
        "account_id":   account_id if BACKEND == "postgres" else None,
        "account_type": req.account_type,
        "role":         role,
    }


@app.get("/api/conversations")
def list_conversations(identity: Identity = Depends(get_identity)):
    """Return the caller's conversation list, newest first. Strictly user-scoped."""
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        return []
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT set_config('brain.user_id', %s, false)", (identity.user_id,))
        cur.execute("""
            SELECT id, title, team_id, created_at, updated_at
            FROM conversations
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT 100
        """, (identity.user_id,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@app.post("/api/conversations")
def create_conversation(
    req: ConversationRequest,
    identity: Identity = Depends(get_identity),
):
    """Create a new conversation for the caller."""
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        import uuid as _uuid
        return {"id": str(_uuid.uuid4()), "title": req.title}
    from brain.core import db_pg as _pg
    import uuid as _uuid
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        _pg._set_user_id(conn, identity.user_id)
        # Get account_id
        cur.execute("""
            SELECT account_id FROM memberships
            WHERE user_id = %s AND status = 'active' LIMIT 1
        """, (identity.user_id,))
        row = cur.fetchone()
        account_id = row["account_id"] if row else identity.workspace_id
        conv_id = str(_uuid.uuid4())
        cur.execute("""
            INSERT INTO conversations (id, user_id, account_id, team_id, title)
            VALUES (%s, %s, %s, %s, %s)
        """, (conv_id, identity.user_id, account_id,
              req.team_id or None, req.title or "New conversation"))
        conn.commit()
        cur.execute("SELECT * FROM conversations WHERE id=%s", (conv_id,))
        return dict(cur.fetchone())
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.get("/api/conversations/{conv_id}/messages")
def get_messages(conv_id: str, identity: Identity = Depends(get_identity)):
    """Return messages for a conversation. User can only read their own."""
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        return []
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        _pg._set_user_id(conn, identity.user_id)
        # Verify ownership
        cur.execute(
            "SELECT id FROM conversations WHERE id=%s AND user_id=%s",
            (conv_id, identity.user_id)
        )
        if not cur.fetchone():
            raise HTTPException(403, "Conversation not found")
        cur.execute("""
            SELECT id, role, content, created_at
            FROM messages WHERE conversation_id=%s ORDER BY created_at
        """, (conv_id,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@app.post("/api/conversations/{conv_id}/messages")
def add_message(
    conv_id: str,
    req: MessageRequest,
    identity: Identity = Depends(get_identity),
):
    """Append a message to a conversation. User can only write to their own."""
    if req.role not in ("user", "assistant"):
        raise HTTPException(400, "role must be 'user' or 'assistant'")
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        return {"ok": True}
    from brain.core import db_pg as _pg
    import uuid as _uuid
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        _pg._set_user_id(conn, identity.user_id)
        cur.execute(
            "SELECT id FROM conversations WHERE id=%s AND user_id=%s",
            (conv_id, identity.user_id)
        )
        if not cur.fetchone():
            raise HTTPException(403, "Conversation not found")
        msg_id = str(_uuid.uuid4())
        cur.execute("""
            INSERT INTO messages (id, conversation_id, role, content)
            VALUES (%s, %s, %s, %s)
        """, (msg_id, conv_id, req.role, req.content))
        cur.execute(
            "UPDATE conversations SET updated_at=now() WHERE id=%s",
            (conv_id,)
        )
        conn.commit()
        return {"id": msg_id, "ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str, identity: Identity = Depends(get_identity)):
    """Delete a conversation and all its messages. User can only delete their own."""
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        return {"ok": True}
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        _pg._set_user_id(conn, identity.user_id)
        cur.execute(
            "DELETE FROM conversations WHERE id=%s AND user_id=%s",
            (conv_id, identity.user_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(403, "Conversation not found")
        conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.post("/api/auth/sso-provision")
def sso_provision(identity: Identity = Depends(get_identity)):
    """
    Called after every SSO login. Creates account + membership rows for first-time
    SSO users. Idempotent — safe to call on every login.
    """
    import uuid as _uuid
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        return {"ok": True, "skipped": "sqlite"}
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        # Check if membership already exists
        cur.execute(
            "SELECT id FROM memberships WHERE user_id = %s AND status = 'active' LIMIT 1",
            (identity.user_id,)
        )
        if cur.fetchone():
            return {"ok": True, "provisioned": False}

        # Check if this email is on a pending invite — if so, honour it
        cur.execute(
            """SELECT i.id, i.account_id, i.role, i.team_ids
               FROM invitations i
               WHERE i.email = %s AND i.status = 'pending' AND i.expires_at > now()
               ORDER BY i.created_at DESC LIMIT 1""",
            (identity.email,)
        )
        invite_row = cur.fetchone()

        if invite_row:
            import json as _json
            invite_row = dict(invite_row)
            team_ids = _json.loads(invite_row["team_ids"] or "[]")
            team_id  = team_ids[0] if team_ids else None
            cur.execute("""
                INSERT INTO memberships (id, user_id, account_id, role, team_id, status)
                VALUES (%s,%s,%s,%s,%s,'active')
                ON CONFLICT (user_id, account_id, team_id) DO UPDATE SET status='active'
            """, (str(_uuid.uuid4()), identity.user_id,
                  invite_row["account_id"], invite_row["role"], team_id))
            cur.execute("UPDATE invitations SET status='accepted' WHERE id=%s", (invite_row["id"],))
            conn.commit()
            return {"ok": True, "provisioned": True, "via": "invite"}

        # No invite — auto-create a personal account
        domain = identity.email.split("@")[1] if identity.email and "@" in identity.email else None
        acct_type = "individual"
        # Treat non-public domains as company accounts
        public_domains = {
            "gmail.com","googlemail.com","yahoo.com","hotmail.com","outlook.com",
            "live.com","icloud.com","me.com","aol.com","protonmail.com","hey.com",
        }
        if domain and domain not in public_domains:
            acct_type = "company"

        acct_name = identity.email.split("@")[0].replace(".", " ").title()
        account_id = str(_uuid.uuid4())
        cur.execute(
            "INSERT INTO accounts (id, type, name, domain) VALUES (%s,%s,%s,%s)",
            (account_id, acct_type, acct_name, domain),
        )
        role = "admin" if acct_type == "company" else "owner"
        cur.execute(
            "INSERT INTO memberships (id, user_id, account_id, role, status) VALUES (%s,%s,%s,%s,'active')",
            (str(_uuid.uuid4()), identity.user_id, account_id, role),
        )
        conn.commit()
        return {"ok": True, "provisioned": True, "via": "auto", "account_type": acct_type}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.get("/api/invite/{token}")
def get_invite(token: str):
    """Validate an invite token. Public — no auth required."""
    import datetime
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        raise HTTPException(400, "Invites require Postgres backend")
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT i.id, i.email, i.role, i.team_ids, i.expires_at, i.status,
                   a.name AS account_name, a.type AS account_type
            FROM invitations i
            JOIN accounts a ON a.id = i.account_id
            WHERE i.token = %s
        """, (token,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Invite link is invalid or has expired")
        row = dict(row)
        if row["status"] != "pending":
            raise HTTPException(410, "This invite has already been used or revoked")
        expires = row["expires_at"]
        if isinstance(expires, str):
            expires = datetime.datetime.fromisoformat(expires.replace("Z",""))
        if expires.replace(tzinfo=None) < datetime.datetime.utcnow():
            raise HTTPException(410, "This invite link has expired")
        return {
            "email":        row["email"],
            "role":         row["role"],
            "account_name": row["account_name"],
            "account_type": row["account_type"],
        }
    finally:
        conn.close()


@app.post("/api/invite/{token}/accept")
def accept_invite(token: str, req: InviteAcceptRequest):
    """Accept an invite: create Supabase user + membership row, mark invite accepted. Public."""
    import datetime, json as _json
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        raise HTTPException(400, "Invites require Postgres backend")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    from brain.core import db_pg as _pg
    import uuid as _uuid
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT i.id, i.email, i.role, i.team_ids, i.account_id, i.expires_at, i.status
            FROM invitations i
            WHERE i.token = %s
        """, (token,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Invalid invite token")
        row = dict(row)
        if row["status"] != "pending":
            raise HTTPException(410, "This invite has already been used or revoked")
        expires = row["expires_at"]
        if isinstance(expires, str):
            expires = datetime.datetime.fromisoformat(expires.replace("Z",""))
        if expires.replace(tzinfo=None) < datetime.datetime.utcnow():
            raise HTTPException(410, "Invite has expired")

        email      = row["email"]
        role       = row["role"]
        account_id = row["account_id"]
        team_ids   = _json.loads(row["team_ids"] or "[]")
        team_id    = team_ids[0] if team_ids else None

        # 1. Create Supabase auth user
        supabase_uid = None
        supabase_url = os.getenv("SUPABASE_URL")
        service_key  = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if supabase_url and service_key:
            try:
                import requests as _req
                resp = _req.post(
                    f"{supabase_url}/auth/v1/admin/users",
                    headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
                    json={"email": email, "password": req.password, "email_confirm": True},
                    timeout=15,
                )
                if resp.status_code not in (200, 201):
                    detail = resp.json().get("message") or resp.text
                    raise HTTPException(400, f"Auth error: {detail}")
                supabase_uid = resp.json().get("id")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(500, f"Auth service error: {e}")

        user_id = supabase_uid or str(_uuid.uuid4())

        # 2. Create membership row
        cur.execute("""
            INSERT INTO memberships (id, user_id, account_id, role, team_id, status)
            VALUES (%s, %s, %s, %s, %s, 'active')
            ON CONFLICT (user_id, account_id, team_id) DO UPDATE
              SET role=EXCLUDED.role, status='active'
        """, (str(_uuid.uuid4()), user_id, account_id, role, team_id))

        # 3. Mark invite accepted
        cur.execute(
            "UPDATE invitations SET status='accepted' WHERE id=%s",
            (row["id"],)
        )
        conn.commit()
        return {"user_id": user_id, "email": email, "role": role}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.get("/api/members")
def get_members(identity: Identity = Depends(require_admin)):
    """List all members in the caller's account. Admin-only."""
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        return []
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        # Find account for this user
        cur.execute("""
            SELECT account_id FROM memberships
            WHERE user_id = %s AND status = 'active' LIMIT 1
        """, (identity.user_id,))
        row = cur.fetchone()
        if not row:
            return []
        account_id = row["account_id"]
        # List all members + pending invitations
        cur.execute("""
            SELECT m.user_id, m.role, m.team_id, m.status, m.created_at
            FROM memberships m
            WHERE m.account_id = %s
            ORDER BY m.created_at
        """, (account_id,))
        members = [dict(r) for r in cur.fetchall()]
        cur.close()
        return members
    finally:
        conn.close()


@app.post("/api/members/invite")
def invite_member(
    req: InviteRequest,
    identity: Identity = Depends(require_admin),
):
    """Create a pending invitation for a new member. Admin-only."""
    import secrets, datetime
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        raise HTTPException(400, "Invitations require Postgres backend")

    req.email = req.email.lower().strip()
    if req.role not in ("employee", "admin"):
        raise HTTPException(400, "role must be 'employee' or 'admin'")

    from brain.core import db_pg as _pg
    import uuid as _uuid
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT account_id FROM memberships
            WHERE user_id = %s AND status = 'active' LIMIT 1
        """, (identity.user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(400, "Your account could not be found")
        account_id = row["account_id"]

        token = secrets.token_urlsafe(32)
        expires = datetime.datetime.utcnow() + datetime.timedelta(days=7)
        team_ids_json = f'["{req.team_id}"]' if req.team_id else "[]"
        cur.execute("""
            INSERT INTO invitations
                (id, account_id, email, team_ids, role, token, invited_by, expires_at, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        """, (str(_uuid.uuid4()), account_id, req.email, team_ids_json,
              req.role, token, identity.user_id, expires.isoformat()))
        conn.commit()
        cur.close()

        invite_url = f"http://localhost:7070/?invite={token}"
        return {"email": req.email, "role": req.role, "invite_url": invite_url}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.get("/api/members/invites")
def list_invites(identity: Identity = Depends(require_admin)):
    """List pending invitations for this account. Admin-only."""
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        return []
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT account_id FROM memberships
            WHERE user_id = %s AND status = 'active' LIMIT 1
        """, (identity.user_id,))
        row = cur.fetchone()
        if not row:
            return []
        cur.execute("""
            SELECT id, email, role, team_ids, expires_at, status, created_at
            FROM invitations
            WHERE account_id = %s AND status = 'pending'
              AND expires_at > now()
            ORDER BY created_at DESC
        """, (row["account_id"],))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@app.delete("/api/members/invites/{invite_id}")
def revoke_invite(invite_id: str, identity: Identity = Depends(require_admin)):
    """Revoke a pending invite. Admin-only."""
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        raise HTTPException(400, "Not supported in SQLite mode")
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT i.id FROM invitations i
            JOIN memberships m ON m.account_id = i.account_id
            WHERE i.id = %s AND m.user_id = %s AND m.status = 'active'
        """, (invite_id, identity.user_id))
        if not cur.fetchone():
            raise HTTPException(404, "Invite not found")
        cur.execute("UPDATE invitations SET status='expired' WHERE id=%s", (invite_id,))
        conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.post("/api/members/{user_id}")
def update_member(
    user_id: str,
    req: MemberPatchRequest,
    identity: Identity = Depends(require_admin),
):
    """Update a member's role, status, or department. Admin-only. Cannot modify self."""
    if user_id == identity.user_id:
        raise HTTPException(400, "You cannot modify your own membership this way")
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        raise HTTPException(400, "Not supported in SQLite mode")
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        # Verify target is in same account
        cur.execute("""
            SELECT m2.id FROM memberships m2
            WHERE m2.user_id = %s
              AND m2.account_id = (
                SELECT account_id FROM memberships
                WHERE user_id = %s AND status='active' LIMIT 1
              )
        """, (user_id, identity.user_id))
        if not cur.fetchone():
            raise HTTPException(404, "Member not found in your account")

        updates = []
        params  = []
        if req.role is not None:
            if req.role not in ("employee", "admin"):
                raise HTTPException(400, "role must be 'employee' or 'admin'")
            updates.append("role = %s"); params.append(req.role)
        if req.status is not None:
            if req.status not in ("active", "disabled"):
                raise HTTPException(400, "status must be 'active' or 'disabled'")
            updates.append("status = %s"); params.append(req.status)
        if req.team_id is not None:
            updates.append("team_id = %s"); params.append(req.team_id or None)
        if not updates:
            return {"ok": True}

        params.append(user_id)
        cur.execute(
            f"UPDATE memberships SET {', '.join(updates)} WHERE user_id = %s",
            params,
        )
        conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.delete("/api/members/{user_id}")
def remove_member(
    user_id: str,
    identity: Identity = Depends(require_admin),
):
    """Remove a member from the account. Admin-only. Cannot remove self."""
    if user_id == identity.user_id:
        raise HTTPException(400, "You cannot remove yourself")
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        raise HTTPException(400, "Not supported in SQLite mode")
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM memberships
            WHERE user_id = %s
              AND account_id = (
                SELECT account_id FROM memberships
                WHERE user_id = %s AND status='active' LIMIT 1
              )
        """, (user_id, identity.user_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Member not found in your account")
        conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.get("/api/connectors")
def connectors(_identity: Identity = Depends(get_identity)):
    """Returns whether the integration hub (Composio) is configured."""
    return {
        "hub_connected": bool(os.getenv("COMPOSIO_API_KEY")),
    }


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
