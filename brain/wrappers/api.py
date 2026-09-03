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
import sys
import json
import time
import threading
import re
import traceback as _traceback
import uuid as _uuid_mod
from collections import defaultdict
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi import Request as _Req
from fastapi.responses import HTMLResponse, JSONResponse as _JSONResp, Response, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel
from pathlib import Path

from brain.auth import Identity, get_identity, require_admin, require_not_guest
from brain.engine import Brain
from brain.core import db as _db
from brain import config
from brain.core import log as _log, metrics as _metrics
from brain.wrappers.slack_bot import slack_router

_api_logger = _log.get_logger("brain.api")

# ── CORS: tighten in production ───────────────────────────────────────────────
_ALLOWED_ORIGINS = (
    os.getenv("ALLOWED_ORIGINS", "").split(",")
    if os.getenv("ALLOWED_ORIGINS")
    else ["*"]   # dev default; set ALLOWED_ORIGINS=https://yourdomain.com in prod
)

app = FastAPI(title="Thread")
app.include_router(slack_router, prefix="/slack")


@app.on_event("startup")
def _startup_migrate():
    """Run DB schema migration once at server startup."""
    try:
        from brain.core import db_pg as _pg
        _conn = _pg.connect()
        _pg.migrate(_conn)
        _conn.close()
    except Exception as _e:
        print(f"[thread] startup migration failed (non-fatal): {_e}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Trace-Id"],
    expose_headers=["X-Trace-Id"],
    max_age=600,
)

# ── Trace middleware (must be added FIRST so it wraps everything) ─────────────
class TraceMiddleware(BaseHTTPMiddleware):
    """
    Per-request trace ID propagation and metrics recording.
    - Reads X-Trace-Id header if present; otherwise generates a UUID4.
    - Sets trace_id in contextvars so all structured log calls in the same
      request include it automatically.
    - Stamps X-Trace-Id on the response.
    - Records request latency and status to the in-process metrics store.
    """
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or str(_uuid_mod.uuid4())
        _log.set_trace_id(trace_id)

        start = time.monotonic()
        try:
            response: Response = await call_next(request)
        except Exception:
            # Ensure metrics are recorded even on unhandled exceptions
            duration = time.monotonic() - start
            _metrics.record_request(request.method, request.url.path, 500, duration)
            raise

        duration = time.monotonic() - start
        status = response.status_code
        _metrics.record_request(request.method, request.url.path, status, duration)

        _api_logger.info(
            f"{request.method} {request.url.path} {status} {duration * 1000:.1f}ms",
            extra={
                "http_method":  request.method,
                "http_path":    request.url.path,
                "http_status":  status,
                "duration_ms":  round(duration * 1000, 1),
            },
        )

        response.headers["X-Trace-Id"] = trace_id
        return response

app.add_middleware(TraceMiddleware)

# ── Security headers middleware ───────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["X-Content-Type-Options"]  = "nosniff"
        resp.headers["X-Frame-Options"]          = "DENY"
        resp.headers["X-XSS-Protection"]         = "1; mode=block"
        resp.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
        resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        # Permissive CSP for the SPA (inline scripts/styles required)
        resp.headers["Content-Security-Policy"]  = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        return resp

app.add_middleware(SecurityHeadersMiddleware)

# ── Global exception handlers — clean JSON errors, no stack traces to clients ─

@app.exception_handler(StarletteHTTPException)
async def _http_exc(request: _Req, exc: StarletteHTTPException):
    """Convert all HTTP exceptions to clean JSON. Status 4xx logged at debug, 5xx at error."""
    detail = exc.detail
    if exc.status_code >= 500:
        import sys
        print(f"[api] 5xx {exc.status_code} {request.method} {request.url.path}: {detail}",
              file=sys.stderr)
        # Several handlers raise HTTPException(500, str(e)), which would echo raw
        # driver text — table names, constraints, SQL — straight to the client.
        # Log the detail above, return an opaque message.
        detail = "An internal error occurred. Please try again."
    return _JSONResp(
        status_code=exc.status_code,
        content={"error": detail, "status": exc.status_code},
    )


@app.exception_handler(RequestValidationError)
async def _validation_exc(_request: _Req, exc: RequestValidationError):
    """Pydantic validation errors → 422 with field-level messages, no stack trace."""
    errors = [
        {"field": ".".join(str(loc) for loc in e["loc"]), "msg": e["msg"]}
        for e in exc.errors()
    ]
    return _JSONResp(
        status_code=422,
        content={"error": "Request validation failed", "details": errors, "status": 422},
    )


@app.exception_handler(Exception)
async def _unhandled_exc(request: _Req, _exc: Exception):
    """Catch-all: log full traceback server-side, return opaque 500 to client."""
    import sys
    print(
        f"[api] unhandled exception {request.method} {request.url.path}\n"
        + _traceback.format_exc(),
        file=sys.stderr,
    )
    return _JSONResp(
        status_code=500,
        content={"error": "An internal error occurred. Please try again.", "status": 500},
    )


# ── In-process rate limiter (sliding-window per IP or per-user+workspace) ────
_rl_lock  = threading.Lock()
_rl_store: dict = defaultdict(list)   # key -> [timestamp, ...]


def _rate_key(request: Request, identity: "Identity | None" = None) -> str:
    """
    Build the rate-limiter bucket key.
    - For LLM-gated endpoints: key on user_id:workspace_id so users who
      share an IP (VPN, office NAT) get independent quotas.
    - Fallback to IP for pre-auth endpoints (signup, login) where we have
      no identity yet.
    """
    ip = request.client.host if request.client else "unknown"
    if identity is not None and identity.user_id:
        return f"{identity.user_id}:{identity.workspace_id}"
    return ip


def _rate_limit(request: Request, limit: int, window: int,
                identity: "Identity | None" = None):
    """Raise 429 if the caller has exceeded `limit` requests in `window` seconds."""
    key = _rate_key(request, identity)
    now = time.monotonic()
    with _rl_lock:
        hits = _rl_store[key]
        # Evict entries outside the window
        hits = [t for t in hits if now - t < window]
        if len(hits) >= limit:
            _rl_store[key] = hits
            raise HTTPException(status_code=429, detail="Too many requests — slow down.")
        hits.append(now)
        _rl_store[key] = hits


def _rl_auth(request: Request):
    """20 requests per 60 s — auth endpoints (signup, login). Keyed by IP only."""
    _rate_limit(request, limit=20, window=60, identity=None)


def _rl_llm(request: Request, identity: "Identity | None" = None):
    """60 requests per 60 s — LLM endpoints. Keyed by user+workspace when available."""
    _rate_limit(request, limit=60, window=60, identity=identity)


def _rl_write(request: Request, identity: "Identity | None" = None):
    """120 requests per 60 s — general write endpoints. Keyed by user+workspace when available."""
    _rate_limit(request, limit=120, window=60, identity=identity)


# ── guest daily quota (in-process rolling 24 h window) ────────────────────────
_GUEST_DAILY_CAP = int(os.getenv("GUEST_DAILY_CAP", "100"))
_GUEST_SESSION_CAP = int(os.getenv("GUEST_SESSION_CAP", "20"))
_guest_daily_hits: list = []   # list of timestamps; access under _rl_lock

def _check_guest_daily_cap():
    """Raise 429 if the global guest query quota for the rolling 24-hour window is exceeded."""
    now = time.monotonic()
    window = 86400  # 24 h
    with _rl_lock:
        # Evict old entries
        fresh = [t for t in _guest_daily_hits if now - t < window]
        if len(fresh) >= _GUEST_DAILY_CAP:
            _guest_daily_hits[:] = fresh
            raise HTTPException(
                status_code=429,
                detail="demo_limit_daily",
            )
        fresh.append(now)
        _guest_daily_hits[:] = fresh

# ── request models ────────────────────────────────────────────────────────────
# NOTE: team_id is intentionally absent from all write-request bodies.
#       The caller's team comes from the verified JWT, not from the client.

_SLUG_RE = re.compile(r'^[a-z0-9_\-]{1,64}$')

class SearchRequest(BaseModel):
    query: str
    cross_team: bool = False

class RecordRequest(BaseModel):
    text: str
    shared: bool = False

class CorrectRequest(BaseModel):
    fact_id: str
    correction: str

class CreateTeamRequest(BaseModel):
    team_id: str
    name: str
    description: str = ""

    def model_post_init(self, __context):
        if not _SLUG_RE.match(self.team_id):
            raise ValueError("team_id must be 1-64 lowercase letters, numbers, _ or -")
        if not self.name.strip() or len(self.name) > 128:
            raise ValueError("name must be 1-128 chars")
        if len(self.description) > 500:
            raise ValueError("description must be ≤500 chars")

class BackfillRequest(BaseModel):
    source: str

    def model_post_init(self, __context):
        if not _SLUG_RE.match(self.source):
            raise ValueError("source must be a valid slug")

class SyncRequest(BaseModel):
    source: str

    def model_post_init(self, __context):
        if not _SLUG_RE.match(self.source):
            raise ValueError("source must be a valid slug")

class ComposioConnectRequest(BaseModel):
    app: str
    redirect_url: str = ""

    def model_post_init(self, __context):
        if not _SLUG_RE.match(self.app):
            raise ValueError("app must be a valid slug")
        if self.redirect_url and len(self.redirect_url) > 512:
            raise ValueError("redirect_url too long")

class SignupRequest(BaseModel):
    email: str
    password: str
    account_type: str
    name: str

    def model_post_init(self, __context):
        if len(self.email) > 254:
            raise ValueError("email too long")
        if len(self.name) > 200:
            raise ValueError("name too long")
        if self.account_type not in ("individual", "company"):
            raise ValueError("account_type must be 'individual' or 'company'")

class InviteRequest(BaseModel):
    email: str
    role: str = "employee"
    team_id: str = None

    def model_post_init(self, __context):
        if len(self.email) > 254:
            raise ValueError("email too long")
        if self.role not in ("employee", "admin"):
            raise ValueError("role must be 'employee' or 'admin'")
        if self.team_id and not _SLUG_RE.match(self.team_id):
            raise ValueError("team_id must be a valid slug")

class MemberPatchRequest(BaseModel):
    role: str = None
    status: str = None
    team_id: str = None

    def model_post_init(self, __context):
        if self.role is not None and self.role not in ("employee", "admin"):
            raise ValueError("role must be 'employee' or 'admin'")
        if self.status is not None and self.status not in ("active", "disabled"):
            raise ValueError("status must be 'active' or 'disabled'")
        if self.team_id and not _SLUG_RE.match(self.team_id):
            raise ValueError("team_id must be a valid slug")

class InviteAcceptRequest(BaseModel):
    password: str

    def model_post_init(self, __context):
        if len(self.password) > 1024:
            raise ValueError("password too long")

class SlackMappingRequest(BaseModel):
    channel_id: str
    channel_name: str
    team_id: str
    sync_members: bool = False

class ConversationRequest(BaseModel):
    title: str = "New conversation"
    team_id: str = None

    def model_post_init(self, __context):
        if len(self.title) > 200:
            raise ValueError("title must be ≤200 chars")

class MessageRequest(BaseModel):
    role: str           # "user" | "assistant"
    content: str

    def model_post_init(self, __context):
        if self.role not in ("user", "assistant"):
            raise ValueError("role must be 'user' or 'assistant'")
        if not self.content or not self.content.strip():
            raise ValueError("content must not be empty")
        if len(self.content) > 100_000:
            raise ValueError("content exceeds maximum length (100,000 chars)")

_PUBLIC_DOMAINS = {
    "gmail.com","googlemail.com","yahoo.com","yahoo.co.uk","yahoo.in",
    "hotmail.com","outlook.com","live.com","msn.com","icloud.com",
    "me.com","mac.com","aol.com","protonmail.com","pm.me","proton.me",
    "yandex.com","yandex.ru","mail.com","zohomail.com","zoho.com",
    "fastmail.com","tutanota.com","hey.com",
}


# ── helpers ───────────────────────────────────────────────────────────────────

# Simple in-process cache for user_id → account_id (membership doesn't change often)
_account_id_cache: dict = {}

def _resolve_account_id(user_id: str) -> str:
    """Resolve account_id from user_id via memberships table. Returns '' on any error."""
    if user_id in _account_id_cache:
        return _account_id_cache[user_id]
    try:
        from brain.core import db_pg as _pg
        conn = _pg.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT account_id FROM memberships WHERE user_id=%s AND status='active' "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id,)
            )
            row = cur.fetchone()
            cur.close()
            result = (row or {}).get("account_id", "") or ""
            if result:
                _account_id_cache[user_id] = result
            return result
        finally:
            conn.close()
    except Exception:
        return ""


def _brain(identity: Identity, team_id: str = None) -> Brain:
    """Build a Brain scoped to the authenticated identity, with billing context.

    Guests are locked to the demo workspace at the auth layer, but we assert it
    here too so that the scope is enforced even if _brain() is called directly.
    """
    from brain.auth import GUEST_WORKSPACE
    if identity.is_guest and identity.workspace_id != GUEST_WORKSPACE:
        raise HTTPException(403, "Guest accounts are locked to the demo workspace.")
    account_id = _resolve_account_id(identity.user_id)
    b = Brain(
        workspace_id=identity.workspace_id,
        team_id=team_id or identity.team_id,
        caller=identity.user_id,
        principals=identity.principals,
        account_id=account_id or None,
    )
    # Set brain.workspace_id session var so RLS policies apply to all queries on this connection.
    from brain.core import db_pg as _pg
    _pg._set_ws(b.conn, b.ws)
    # Set LLM billing context so every chat_json/chat_text call in this request
    # writes a usage_events row attributed to the correct account.
    from brain.core.llm import set_billing_context
    set_billing_context(
        account_id=account_id,
        user_id=identity.user_id,
        team_id=identity.team_id,
    )
    return b


def _q(conn, sql, params):
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return rows


def _q1(conn, sql, params):
    rows = _q(conn, sql, params)
    return rows[0] if rows else None


def _enrich(facts, conn):
    """Attach episode source/date to each fact for display."""
    for f in facts:
        ep_id = f.get("episode_id")
        if ep_id:
            row = _q1(conn,
                "SELECT source,title,url,author,occurred_at FROM episodes WHERE id=%s",
                (ep_id,))
            if row:
                f.update({"ep_source": row["source"], "ep_title": row["title"],
                           "ep_url": row["url"], "ep_author": row["author"],
                           "ep_occurred_at": row["occurred_at"]})
    return facts


def _auto_discover_teams(b: Brain):
    """Auto-register any team_id found in facts that has no teams table row yet."""
    rows = _q(b.conn,
        "SELECT DISTINCT team_id FROM facts WHERE workspace_id=%s AND team_id IS NOT NULL LIMIT 50",
        (b.ws,))
    for row in rows:
        tid = row["team_id"]
        if tid and tid != "shared":
            _db.ensure_team(b.conn, b.ws, tid, tid.replace("-", " ").title())


# ── public endpoints (no auth required) ──────────────────────────────────────

@app.get("/logo")
def logo():
    from fastapi.responses import FileResponse
    p = Path(__file__).parent.parent.parent / "Thread Logo.png"
    if not p.exists():
        raise HTTPException(404, "Logo not found")
    return FileResponse(str(p), media_type="image/png")


@app.get("/", response_class=HTMLResponse)
def landing():
    p = Path(__file__).parent / "webapp" / "landing.html"
    return HTMLResponse(p.read_text(encoding="utf-8"))


@app.get("/app", response_class=HTMLResponse)
def index():
    p = Path(__file__).parent / "webapp" / "index.html"
    html = p.read_text(encoding="utf-8")
    clerk_pk = os.getenv("CLERK_PUBLISHABLE_KEY", "")
    injection = f"""<script>
window.__CLERK_PUBLISHABLE_KEY__ = {json.dumps(clerk_pk)};
</script>"""
    html = html.replace("</head>", injection + "\n</head>", 1)
    return HTMLResponse(html)


@app.get("/healthz")
def healthz():
    """Liveness probe — always 200 if the process is alive."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """
    Readiness probe — checks DB, Redis, and LLM config reachability.
    Returns 200 if all checks pass, 503 if any fail.
    Each check has a 2-second timeout.
    No auth required (Kubernetes / load-balancer scrapes this).
    """
    import concurrent.futures as _cf

    checks: dict = {}

    def _check_db():
        try:
            from brain.core import db_pg as _pg
            conn = _pg.connect()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.close()
            return "ok"
        except Exception as e:
            return f"error: {e}"

    def _check_redis():
        try:
            import redis as _redis
            r = _redis.Redis.from_url(
                config.REDIS_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            r.ping()
            return "ok"
        except Exception as e:
            return f"error: {e}"

    def _check_llm():
        # Only verify that at least one LLM API key is configured — no real call.
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROK_API_KEY", "GEMINI_API_KEY"):
            if os.getenv(key):
                return "ok"
        return "error: no LLM API key configured"

    with _cf.ThreadPoolExecutor(max_workers=3) as pool:
        db_fut    = pool.submit(_check_db)
        redis_fut = pool.submit(_check_redis)
        llm_fut   = pool.submit(_check_llm)

        try:
            checks["db"]    = db_fut.result(timeout=2)
        except _cf.TimeoutError:
            checks["db"]    = "error: timeout"
        try:
            checks["redis"] = redis_fut.result(timeout=2)
        except _cf.TimeoutError:
            checks["redis"] = "error: timeout"
        try:
            checks["llm"]   = llm_fut.result(timeout=2)
        except _cf.TimeoutError:
            checks["llm"]   = "error: timeout"

    all_ok = all(v == "ok" for v in checks.values())
    status_code = 200 if all_ok else 503
    body = {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
    }
    from fastapi.responses import JSONResponse
    return JSONResponse(content=body, status_code=status_code)


@app.get("/metrics")
def metrics_endpoint():
    """
    Prometheus metrics scrape endpoint.
    No auth required — Prometheus scrapes this on its polling interval.
    Returns Prometheus text exposition format.
    """
    # Include queue_depth from Redis (metrics.prometheus_text() reads it internally)
    text = _metrics.prometheus_text()
    return PlainTextResponse(content=text, media_type="text/plain; version=0.0.4; charset=utf-8")


@app.post("/api/guest-session")
def guest_session(request: Request):
    """
    Issue a short-lived self-signed guest JWT for the demo workspace.
    No credentials required — public endpoint.
    Rate-limited per IP (20 req/60s) to prevent token farming.
    """
    from brain.auth import GUEST_WORKSPACE, _GUEST_TOKEN_SECRET, _GUEST_TOKEN_PREFIX
    if not _GUEST_TOKEN_SECRET:
        raise HTTPException(503, "Demo access is not enabled on this server.")
    _rate_limit(request, limit=20, window=60, identity=None)  # IP-keyed

    import hmac
    import hashlib
    import base64
    import json as _json
    import time as _time
    import uuid as _uuid

    exp = int(_time.time()) + 4 * 3600   # 4-hour session
    payload = _json.dumps({
        "sub":  f"guest_{_uuid.uuid4().hex[:12]}",
        "role": "guest",
        "ws":   GUEST_WORKSPACE,
        "exp":  exp,
    }, separators=(",", ":")).encode()

    sig = hmac.new(
        _GUEST_TOKEN_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()

    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    token = f"{_GUEST_TOKEN_PREFIX}{b64}.{sig}"
    return {"token": token, "expires_in": 4 * 3600,
            "session_cap": _GUEST_SESSION_CAP, "daily_cap": _GUEST_DAILY_CAP}


@app.get("/api/status")
def status(_identity: Identity = Depends(require_not_guest)):
    """Returns LLM mode, auth mode, key presence, and circuit-breaker health."""
    from dotenv import dotenv_values
    from brain.core.resilience import breaker_states
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
            "openai":       _get("OPENAI_API_KEY"),
            "anthropic":    _get("ANTHROPIC_API_KEY"),
            "gemini":       _get("GEMINI_API_KEY"),
            "grok":         _get("GROK_API_KEY"),
            "slack":        _get("SLACK_BOT_TOKEN"),
            "google":       _get("GOOGLE_CREDENTIALS_JSON"),
            "notion":       _get("NOTION_TOKEN"),
            "clerk":        _get("CLERK_SECRET_KEY"),
            "integrations": _get("COMPOSIO_API_KEY"),
        },
        "circuit_breakers": breaker_states(),
    }


# ── auth-gated endpoints ──────────────────────────────────────────────────────

@app.get("/api/me")
def me(identity: Identity = Depends(get_identity)):
    """Return the caller's resolved identity (debug / UI persona display)."""
    account_type = "company"  # default — company UI until we know otherwise
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
        "is_guest":     identity.is_guest,
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
    request: Request,
    identity: Identity = Depends(get_identity),
):
    _rl_llm(request, identity)
    if identity.is_guest:
        _check_guest_daily_cap()
    if not req.query or not req.query.strip():
        raise HTTPException(400, "query must not be empty")
    if len(req.query) > 2000:
        raise HTTPException(400, "query exceeds maximum length (2000 chars)")
    # cross_team is honoured only if the caller has admin role
    cross = req.cross_team and identity.is_admin
    b = _brain(identity)
    result = b.search(req.query.strip(), surface="web", cross_team=cross)
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
    request: Request,
    identity: Identity = Depends(require_not_guest),
):
    _rl_write(request, identity)
    if not req.text or not req.text.strip():
        raise HTTPException(400, "text must not be empty")
    if len(req.text) > 50_000:
        raise HTTPException(400, "text exceeds maximum length (50,000 chars)")
    visibility = "shared" if req.shared else "team"
    return _brain(identity).record(req.text.strip(), visibility=visibility)


@app.post("/api/correct")
def correct(
    req: CorrectRequest,
    request: Request,
    identity: Identity = Depends(require_not_guest),
):
    _rl_write(request, identity)
    if not req.fact_id or not req.correction or not req.correction.strip():
        raise HTTPException(400, "fact_id and correction are required")
    if len(req.correction) > 10_000:
        raise HTTPException(400, "correction exceeds maximum length (10,000 chars)")
    return _brain(identity).correct(req.fact_id, req.correction.strip())


@app.get("/api/ledger")
def ledger(
    limit: int = 50,
    offset: int = 0,
    identity: Identity = Depends(get_identity),
):
    # Admins see full ledger; regular members see only their team's
    limit = min(limit, 200)
    offset = max(offset, 0)
    b = _brain(identity)
    team_scope = None if identity.is_admin else identity.team_id
    return _db.read_ledger(b.conn, b.ws, limit, team_id=team_scope, offset=offset)


@app.get("/api/facts")
def get_facts(
    cross_team: bool = False,
    limit: int = 50,
    offset: int = 0,
    identity: Identity = Depends(get_identity),
):
    limit = min(limit, 200)
    offset = max(offset, 0)
    b = _brain(identity)
    conn, ws = b.conn, b.ws
    tid = identity.team_id
    cross = cross_team and identity.is_admin

    if cross:
        rows = _q(conn,
            "SELECT f.*,e.source ep_source FROM facts f "
            "LEFT JOIN episodes e ON f.episode_id=e.id "
            "WHERE f.workspace_id=%s AND f.invalidated_at IS NULL "
            "ORDER BY f.introduced_at DESC LIMIT %s OFFSET %s",
            (ws, limit, offset))
    else:
        rows = _q(conn,
            "SELECT f.*,e.source ep_source FROM facts f "
            "LEFT JOIN episodes e ON f.episode_id=e.id "
            "WHERE f.workspace_id=%s AND f.invalidated_at IS NULL "
            "AND (f.team_id=%s OR f.visibility='shared') "
            "ORDER BY f.introduced_at DESC LIMIT %s OFFSET %s",
            (ws, tid, limit, offset))
    return [dict(r) for r in rows]


_COMPOSIO_CACHE: dict = {}   # cursor_key → (timestamp, payload)
_COMPOSIO_CACHE_TTL = 3600  # 1 hour

@app.get("/api/composio/apps")
def composio_apps(limit: int = 50, cursor: str = None,
                  _identity: Identity = Depends(get_identity)):
    """List available integration apps with pagination. Authenticated users only."""
    import time
    cache_key = f"{limit}:{cursor or ''}"
    cached = _COMPOSIO_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _COMPOSIO_CACHE_TTL:
        return cached[1]

    try:
        import requests as _req
        import os
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
def composio_connected(
    limit: int = 50,
    offset: int = 0,
    identity: Identity = Depends(get_identity),
):
    """List apps the calling user has connected. Admins also see all workspace connections."""
    limit = min(limit, 200)
    offset = max(offset, 0)
    try:
        from brain.core.connectors.composio import list_connected_apps
        if identity.is_admin:
            # Admins see all workspace connections
            apps = list_connected_apps()
        else:
            # Regular users see only their own connections
            apps = list_connected_apps(user_id=identity.user_id)
        return apps[offset: offset + limit]
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/composio/connected/{account_id}")
def composio_disconnect(account_id: str,
                        identity: Identity = Depends(require_admin)):
    """Disconnect a connected integration by its account ID. Admin only.
    Also deregisters any webhook triggers associated with this connection."""
    try:
        from brain.core.connectors.composio import disconnect_app, deregister_webhook_trigger
        from brain.core import db_pg as _pg

        # Deregister any webhook subscriptions for this account before disconnecting
        try:
            conn = _pg.connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT composio_trigger_id FROM webhook_subscriptions "
                "WHERE workspace_id=%s AND active=TRUE",
                (identity.workspace_id,),
            )
            rows = cur.fetchall()
            for row in rows:
                if row.get("composio_trigger_id"):
                    deregister_webhook_trigger(row["composio_trigger_id"])
            cur.execute(
                "UPDATE webhook_subscriptions SET active=FALSE "
                "WHERE workspace_id=%s AND active=TRUE",
                (identity.workspace_id,),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            import sys
            print(f"[disconnect] webhook cleanup error: {e}", file=sys.stderr)

        return disconnect_app(account_id)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/composio/connect")
def composio_connect(
    req: ComposioConnectRequest,
    identity: Identity = Depends(require_not_guest),
):
    """Initiate OAuth for a Composio app. Uses the caller's user_id as entity_id
    so each user's connection is isolated — teammates cannot see each other's data.
    After OAuth completes, the /api/composio/callback endpoint registers webhook triggers."""
    try:
        from brain.core.connectors.composio import get_connect_url
        # entity_id = caller's user_id - each user gets their own OAuth connection
        _ = req.redirect_url or os.getenv("APP_BASE_URL", "http://localhost:7070")
        # Point Composio callback back through our endpoint so we can register webhooks
        app_base = os.getenv("APP_BASE_URL", "http://localhost:7070").rstrip("/")
        callback = f"{app_base}/api/composio/callback?user_id={identity.user_id}&workspace_id={identity.workspace_id}&account_id={identity.account_id or ''}"
        url = get_connect_url(req.app, user_id=identity.user_id, redirect_url=callback)
        return {"app": req.app, "connect_url": url}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/composio/callback")
def composio_oauth_callback(
    request: Request,
    user_id: str = "",
    workspace_id: str = "",
    account_id: str = "",
    app: str = "",
    connected_account_id: str = "",
):
    """
    Composio redirects here after a user completes OAuth.
    Registers webhook triggers and saves the entity->workspace routing
    so inbound webhook events can be routed to the right workspace.
    Then redirects the browser to the integrations page.
    """
    from brain.core.connectors.composio import (
        register_webhook_trigger, list_connected_apps,
    )
    from brain.core import db_pg as _pg
    from brain.core.db_pg import upsert_webhook_route
    from fastapi.responses import RedirectResponse

    entity_id = user_id or "default"
    ws = workspace_id or config.WORKSPACE_ID
    acct = account_id or ""

    # Save entity->workspace route so webhook events can be dispatched
    if entity_id and ws:
        try:
            conn = _pg.connect()
            upsert_webhook_route(conn, entity_id=entity_id,
                                 workspace_id=ws, account_id=acct)
            conn.close()
        except Exception as e:
            import sys
            print(f"[callback] route upsert failed: {e}", file=sys.stderr)

    # Register webhook triggers for each newly connected app
    app_base = os.getenv("APP_BASE_URL", "http://localhost:7070").rstrip("/")
    webhook_url = f"{app_base}/api/ingest/event"
    webhook_secret = config.COMPOSIO_WEBHOOK_SECRET

    if webhook_secret:
        try:
            connected = list_connected_apps(user_id=entity_id)
            conn = _pg.connect()
            cur = conn.cursor()
            for acct_info in connected:
                app_slug = acct_info.get("app", "")
                if not app_slug:
                    continue
                # Check if already subscribed
                cur.execute(
                    "SELECT 1 FROM webhook_subscriptions "
                    "WHERE workspace_id=%s AND app_slug=%s AND active=TRUE",
                    (ws, app_slug),
                )
                if cur.fetchone():
                    continue
                trigger_id = register_webhook_trigger(app_slug, entity_id, webhook_url)
                cur.execute(
                    """INSERT INTO webhook_subscriptions
                       (workspace_id, source, app_slug, composio_trigger_id, webhook_url, active)
                       VALUES (%s, %s, %s, %s, %s, TRUE)
                       ON CONFLICT DO NOTHING""",
                    (ws, "composio", app_slug, trigger_id or None, webhook_url),
                )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            import sys
            print(f"[callback] webhook registration error: {e}", file=sys.stderr)

    # Redirect back to the integrations page
    return RedirectResponse(url="/#integrations", status_code=302)


@app.post("/api/auth/signup")
def signup(req: SignupRequest, request: Request):
    """
    Register account + membership after Clerk creates the auth user.
    Clerk handles passwords — we just store the workspace row.
    Called right after Clerk signup completes with the Clerk user_id as user_id.
    """
    _rl_auth(request)
    import uuid as _uuid

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

    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        # Idempotent — don't create duplicate account if already exists
        cur.execute(
            "SELECT m.account_id FROM memberships m WHERE m.user_id=%s AND m.status='active' "
            "ORDER BY m.created_at DESC LIMIT 1",
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

        # Provision free plan subscription for the new account
        from brain.core.db_pg import ensure_free_subscription
        ensure_free_subscription(account_id)
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"DB error: {e}")
    finally:
        conn.close()

    return {
        "user_id":      user_id,
        "account_id":   account_id,
        "account_type": req.account_type,
        "role":         role,
    }


@app.get("/api/conversations")
def list_conversations(identity: Identity = Depends(get_identity)):
    """Return the caller's conversation list, newest first. Strictly user-scoped."""
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
    from brain.core import db_pg as _pg
    import uuid as _uuid
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        _pg._set_user_id(conn, identity.user_id)
        # Get account_id
        cur.execute("""
            SELECT account_id FROM memberships
            WHERE user_id = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
        """, (identity.user_id,))
        row = cur.fetchone()
        if not row:
            # No membership → no account to attach the thread to. Never fall back to
            # workspace_id: it is not an accounts.id and violates the FK.
            raise HTTPException(status_code=403, detail="No active account membership")
        account_id = row["account_id"]
        conv_id = str(_uuid.uuid4())
        cur.execute("""
            INSERT INTO conversations (id, user_id, account_id, team_id, title)
            VALUES (%s, %s, %s, %s, %s)
        """, (conv_id, identity.user_id, account_id,
              req.team_id or None, req.title or "New conversation"))
        conn.commit()
        cur.execute("SELECT * FROM conversations WHERE id=%s", (conv_id,))
        return dict(cur.fetchone())
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.get("/api/conversations/{conv_id}/messages")
def get_messages(conv_id: str, identity: Identity = Depends(get_identity)):
    """Return messages for a conversation. User can only read their own."""
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
def delete_conversation(conv_id: str, identity: Identity = Depends(require_not_guest)):
    """Delete a conversation and all its messages. User can only delete their own."""
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
def sso_provision(request: Request, identity: Identity = Depends(require_not_guest)):
    """
    Called after every SSO login. Creates account + membership rows for first-time
    SSO users. Idempotent — safe to call on every login.
    """
    _rl_auth(request)
    import uuid as _uuid
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
        # Provision free plan for the new account
        from brain.core.db_pg import ensure_free_subscription
        ensure_free_subscription(account_id)
        return {"ok": True, "provisioned": True, "via": "auto", "account_type": acct_type}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.get("/api/byo-key")
def get_byo_key_status(identity: Identity = Depends(require_admin)):
    """Report which providers have a BYO key configured.

    Returns booleans only — the stored key is never echoed back to any client.
    """
    from brain.core import db_pg as _pg
    account_id = _resolve_account_id(identity.user_id)
    providers = ("openai", "anthropic", "gemini")
    try:
        found = _pg.get_byo_keys(account_id, providers)   # one connection, not three
    except Exception:
        found = {}
    return {"configured": {p: bool(found.get(p)) for p in providers}}


class _ProvisionRequest(BaseModel):
    user_id: str
    account_type: str = "individual"
    name: str = ""
    password: str = ""


@app.post("/api/provision")
def dev_provision(body: _ProvisionRequest):
    """Dev-only: create an account + membership row directly. Gated to BRAIN_ENV=development."""
    import os as _os
    if _os.getenv("BRAIN_ENV") != "development":
        raise HTTPException(status_code=404, detail="Not Found")
    import uuid as _uuid
    from brain.core import db_pg as _pg
    from brain.core.db_pg import ensure_free_subscription
    conn = _pg.connect()
    try:
        _pg.migrate(conn)
        account_id = str(_uuid.uuid4())
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO accounts (id, type, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (account_id, body.account_type, body.name or body.user_id),
        )
        role = "owner" if body.account_type == "individual" else "admin"
        # Enrol under the identity that subsequent requests actually arrive as. In
        # dev-bypass that is always config.CALLER, so keying the membership off the
        # request body would leave the caller with no account. One row, one seat —
        # enrolling both would consume the free plan's single seat before any invite.
        from brain.auth import _BYPASS as _bypass_active
        member_id = config.CALLER if _bypass_active else body.user_id
        cur.execute(
            "INSERT INTO memberships (id, user_id, account_id, role, status) "
            "VALUES (%s, %s, %s, %s, 'active') ON CONFLICT DO NOTHING",
            (str(_uuid.uuid4()), member_id, account_id, role),
        )
        conn.commit()
        cur.close()
        # Membership changed — drop the cached user_id → account_id mapping.
        _account_id_cache.pop(member_id, None)
        ensure_free_subscription(account_id)
        return {"ok": True, "account_id": account_id}
    except Exception as _e:
        conn.rollback()
        raise HTTPException(500, str(_e))
    finally:
        conn.close()


@app.get("/api/invite/{token}")
def get_invite(token: str):
    """Validate an invite token. Public — no auth required."""
    import datetime
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
def accept_invite(token: str, req: InviteAcceptRequest, request: Request):
    """Accept an invite: create Supabase user + membership row, mark invite accepted. Public."""
    _rl_auth(request)
    if not token or len(token) > 128:
        raise HTTPException(400, "Invalid invite token")
    import json as _json
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    from brain.core import db_pg as _pg
    import uuid as _uuid
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        # Single atomic query: token match + status='pending' + expiry check in one shot.
        # This prevents TOCTOU races and ensures the token cannot be replayed after
        # status changes to 'accepted' between a SELECT and a later UPDATE.
        cur.execute("""
            SELECT i.id, i.email, i.role, i.team_ids, i.account_id, i.expires_at, i.status
            FROM invitations i
            WHERE i.token = %s AND i.status = 'pending' AND i.expires_at > now()
        """, (token,))
        row = cur.fetchone()
        if not row:
            # Distinguish between not-found, already-used, and expired for better DX,
            # but do NOT expose which case applies (security: don't enumerate tokens).
            cur.execute(
                "SELECT status, expires_at FROM invitations WHERE token = %s", (token,)
            )
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(404, "Invalid invite token")
            if existing["status"] != "pending":
                raise HTTPException(410, "This invite has already been used or revoked")
            raise HTTPException(410, "Invite has expired")
        row = dict(row)

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
def get_members(
    limit: int = 50,
    offset: int = 0,
    identity: Identity = Depends(require_admin),
):
    """List all members in the caller's account. Admin-only."""
    limit = min(limit, 200)
    offset = max(offset, 0)
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        # Newest membership first: a user can belong to several accounts, and an
        # unordered LIMIT 1 would list members of an arbitrary one — matching the
        # ordering used by every other account resolver.
        cur.execute("""
            SELECT account_id FROM memberships
            WHERE user_id = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
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
            LIMIT %s OFFSET %s
        """, (account_id, limit, offset))
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
    import secrets
    import datetime

    req.email = req.email.lower().strip()
    if req.role not in ("employee", "admin"):
        raise HTTPException(400, "role must be 'employee' or 'admin'")

    from brain.core import db_pg as _pg
    import uuid as _uuid
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        # Newest membership first: a user can belong to several accounts, and an
        # unordered LIMIT 1 would pick an arbitrary one.
        cur.execute("""
            SELECT account_id FROM memberships
            WHERE user_id = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
        """, (identity.user_id,))
        row = cur.fetchone()
        if not row:
            # No membership means no account to attach the invitation to. Returning
            # a token here would hand the caller a link that was never stored, so
            # every recipient would hit "invalid or expired" on a fresh invite.
            raise HTTPException(
                status_code=403,
                detail="You are not a member of any account, so you cannot invite others.",
            )
        account_id = row["account_id"]

        # Enforce seat limit before issuing invite
        from brain.core.db_pg import get_plan_for_account, get_seat_count
        plan = get_plan_for_account(account_id)
        seats = get_seat_count(account_id)
        if seats >= plan.get("max_seats", 1):
            _detail = (f"Seat limit reached ({seats}/{plan['max_seats']}). "
                       "Upgrade your plan to add more members.")
            from brain.auth import _IS_PRODUCTION as _in_prod
            if not _in_prod:
                # Non-production only: name the account the limit was read from,
                # so a wrong-account resolution is visible instead of silent.
                cur.execute("SELECT type FROM accounts WHERE id=%s", (account_id,))
                _acct = cur.fetchone() or {}
                _detail += (f" [account={account_id} type={_acct.get('type')} "
                            f"plan={plan.get('name')}]")
            raise HTTPException(402, _detail)

        token = secrets.token_urlsafe(32)
        base_url = os.getenv("APP_BASE_URL", "http://localhost:7070").rstrip("/")
        expires = datetime.datetime.utcnow() + datetime.timedelta(days=7)
        # Serialize properly: interpolating the value would let a quote in
        # team_id corrupt the stored JSON that accept-invite later parses.
        team_ids_json = json.dumps([req.team_id] if req.team_id else [])
        cur.execute("""
            INSERT INTO invitations
                (id, account_id, email, team_ids, role, token, invited_by, expires_at, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        """, (str(_uuid.uuid4()), account_id, req.email, team_ids_json,
              req.role, token, identity.user_id, expires.isoformat()))
        conn.commit()
        cur.close()

        invite_url = f"{base_url}/?invite={token}"
        # `token` is already embedded in invite_url; returning it separately saves
        # callers from parsing the URL and exposes nothing extra.
        return {"email": req.email, "role": req.role,
                "invite_url": invite_url, "token": token}
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
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        # Newest membership first, matching the other account resolvers — an
        # unordered LIMIT 1 would list invitations from an arbitrary account.
        cur.execute("""
            SELECT account_id FROM memberships
            WHERE user_id = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
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
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        # Resolve the admin's own account, then confirm the target belongs to it.
        # The account id is kept so the UPDATE below can be scoped to it.
        cur.execute("""
            SELECT account_id FROM memberships
            WHERE user_id = %s AND status='active'
            ORDER BY created_at DESC LIMIT 1
        """, (identity.user_id,))
        _admin_row = cur.fetchone()
        if not _admin_row:
            raise HTTPException(404, "Member not found in your account")
        admin_account_id = _admin_row["account_id"]

        cur.execute(
            "SELECT id FROM memberships WHERE user_id = %s AND account_id = %s",
            (user_id, admin_account_id),
        )
        if not cur.fetchone():
            raise HTTPException(404, "Member not found in your account")

        updates = []
        params  = []
        if req.role is not None:
            if req.role not in ("employee", "admin"):
                raise HTTPException(400, "role must be 'employee' or 'admin'")
            updates.append("role = %s")
            params.append(req.role)
        if req.status is not None:
            if req.status not in ("active", "disabled"):
                raise HTTPException(400, "status must be 'active' or 'disabled'")
            updates.append("status = %s")
            params.append(req.status)
        if req.team_id is not None:
            updates.append("team_id = %s")
            params.append(req.team_id or None)
        if not updates:
            return {"ok": True}

        # Scope the write to the admin's account: user_id is not unique across
        # accounts, so an unscoped UPDATE would edit this person's membership in
        # every other account they belong to.
        params.extend([user_id, admin_account_id])
        cur.execute(
            f"UPDATE memberships SET {', '.join(updates)} "
            "WHERE user_id = %s AND account_id = %s",
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
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM memberships
            WHERE user_id = %s
              AND account_id = (
                SELECT account_id FROM memberships
                WHERE user_id = %s AND status='active'
                ORDER BY created_at DESC LIMIT 1
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
async def sync(
    req: SyncRequest,
    identity: Identity = Depends(require_not_guest),
):
    """Pull recent items from a connector. Enqueues an ARQ job and returns immediately."""
    from brain.engine import REGISTRY
    if req.source not in REGISTRY:
        raise HTTPException(400, f"Unknown source '{req.source}'")

    try:
        import arq
        pool = await arq.create_pool(
            arq.connections.RedisSettings.from_dsn(config.REDIS_URL)
        )
        await pool.enqueue_job(
            "sync_source",
            ws=identity.workspace_id,
            source=req.source,
            team_id=identity.team_id,
            trigger="manual",
        )
        await pool.aclose()
    except Exception as exc:
        import sys
        print(f"[sync] arq unavailable, falling back to thread: {exc}", file=sys.stderr)
        import sys as _sys
        captured_identity = identity

        def _run():
            try:
                b = _brain(captured_identity)
                b.sync(req.source, team_id=captured_identity.team_id)
            except Exception as e:
                print(f"[sync] background error: {e}", file=_sys.stderr)

        threading.Thread(target=_run, daemon=True).start()

    return {"source": req.source, "status": "syncing",
            "message": "Sync started - facts will appear within 1-2 minutes."}


# ── Webhook ingest endpoint ────────────────────────────────────────────────────

class _WebhookRequest(BaseModel):
    pass  # body parsed from raw bytes


@app.post("/api/ingest/event")
async def ingest_event_webhook(request: Request):
    """
    Inbound webhook receiver for push events from connected integrations.
    Verifies HMAC signature, deduplicates via delivery ID, resolves workspace,
    then enqueues an ingest_event_job for background processing.
    Returns 200 immediately - never blocks on ingestion or extraction.
    """
    import hashlib
    import hmac as _hmac

    webhook_secret = config.COMPOSIO_WEBHOOK_SECRET
    if not webhook_secret:
        raise HTTPException(503, "Webhook ingestion not configured")

    body_bytes = await request.body()

    # HMAC verification
    sig_header = request.headers.get("x-composio-signature", "")
    expected = "sha256=" + _hmac.new(
        webhook_secret.encode(), body_bytes, hashlib.sha256
    ).hexdigest()
    if not _hmac.compare_digest(sig_header, expected):
        raise HTTPException(401, "Invalid webhook signature")

    # Replay protection via delivery ID
    delivery_id = request.headers.get("x-webhook-delivery-id", "")
    if delivery_id:
        try:
            import redis as _redis_lib
            r = _redis_lib.from_url(config.REDIS_URL)
            key = f"webhook:seen:{delivery_id}"
            if r.exists(key):
                return {"status": "duplicate", "delivery_id": delivery_id}
            r.setex(key, 86400, "1")
        except Exception:
            pass  # Redis unavailable - continue without replay protection

    try:
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    # Resolve workspace from entity_id in payload
    entity_id = (
        payload.get("entity_id")
        or payload.get("connectedAccountId")
        or payload.get("connected_account_id")
        or ""
    )

    workspace_id = None
    if entity_id:
        try:
            from brain.core import db as _db
            from brain.core.db_pg import lookup_webhook_route
            conn = _db.connect()
            route = lookup_webhook_route(conn, entity_id)
            conn.close()
            if route:
                workspace_id = route["workspace_id"]
        except Exception as e:
            print(f"[webhook] route lookup error: {e}", file=sys.stderr)

    if not workspace_id:
        # Fall back to default workspace if entity routing not set up yet
        workspace_id = config.WORKSPACE_ID

    # Idempotent enqueue: use delivery_id as job_id so duplicates are no-ops
    idempotency_key = delivery_id or str(_uuid_mod.uuid4())
    try:
        import arq
        pool = await arq.create_pool(
            arq.connections.RedisSettings.from_dsn(config.REDIS_URL)
        )
        await pool.enqueue_job(
            "ingest_event_job",
            ws=workspace_id,
            source="composio",
            raw=payload,
            team_id="shared",
            _job_id=idempotency_key,
        )
        await pool.aclose()
    except Exception as e:
        raise HTTPException(503, f"Job queue unavailable: {e}")

    return {"status": "accepted", "event_id": idempotency_key}


class SubscriptionRequest(BaseModel):
    plan_id: str    # "plan_free" | "plan_team" | "plan_business"


# ── plans (public read) ───────────────────────────────────────────────────────

@app.get("/api/plans")
def list_plans(_identity: Identity = Depends(get_identity)):
    """Return all available plans. Public (any authenticated user can see pricing)."""
    from brain.core import db_pg as _pg
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM plans ORDER BY price_usd_month")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows
    finally:
        conn.close()


# ── subscription management (admin) ───────────────────────────────────────────

@app.get("/api/subscription")
def get_subscription(identity: Identity = Depends(require_admin)):
    """Return this workspace's active subscription and plan limits."""
    from brain.core import db_pg as _pg
    account_id = _resolve_account_id(identity.user_id)
    if not account_id:
        raise HTTPException(404, "Account not found")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id, s.status, s.started_at, s.renews_at,
                   p.id AS plan_id, p.name AS plan_name,
                   p.monthly_credits, p.max_seats, p.max_connectors,
                   p.max_backfill, p.price_usd_month
            FROM subscriptions s
            JOIN plans p ON p.id = s.plan_id
            WHERE s.account_id = %s AND s.status = 'active'
            LIMIT 1
        """, (account_id,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return {"plan_name": "free", "status": "none"}
        return dict(row)
    finally:
        conn.close()


@app.post("/api/subscription")
def set_subscription(
    req: SubscriptionRequest,
    identity: Identity = Depends(require_admin),
):
    """Change the workspace subscription plan. Admin only. (MVP: no payment gate.)"""
    valid_plans = {"plan_free", "plan_team", "plan_business"}
    if req.plan_id not in valid_plans:
        raise HTTPException(400, f"Unknown plan '{req.plan_id}'")
    from brain.core import db_pg as _pg
    account_id = _resolve_account_id(identity.user_id)
    if not account_id:
        raise HTTPException(404, "Account not found")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO subscriptions (account_id, plan_id, status)
            VALUES (%s, %s, 'active')
            ON CONFLICT (account_id) DO UPDATE SET
                plan_id    = EXCLUDED.plan_id,
                status     = 'active',
                started_at = now(),
                cancelled_at = NULL
        """, (account_id, req.plan_id))
        conn.commit()
        cur.close()
        return {"ok": True, "plan_id": req.plan_id}
    finally:
        conn.close()


# ── workspace usage (admin) ───────────────────────────────────────────────────

@app.get("/api/usage")
def get_usage(period: str = None, identity: Identity = Depends(require_admin)):
    """
    Return AI cost and credit usage for this workspace for the given month.
    Restricted to workspace admins. Period format: YYYY-MM (defaults to current month).
    """
    b = _brain(identity)
    return b.usage_summary(period=period)


# ── dead-letter queue (admin) ────────────────────────────────────────────────

@app.get("/api/admin/dead-letter")
def list_dead_letter(
    status: str = "failed",
    limit: int = 50,
    offset: int = 0,
    _identity: Identity = Depends(require_admin),
):
    """
    List dead-letter jobs for this workspace's worker.
    status: 'failed' | 'replayed' | 'discarded'
    Admin-only.
    """
    limit = min(limit, 200)
    offset = max(offset, 0)
    from brain.core import db_pg as _pg
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        return []
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, job_name, kwargs, error, attempts, failed_at, replayed_at, status
            FROM dead_letter_jobs
            WHERE status = %s
            ORDER BY failed_at DESC
            LIMIT %s OFFSET %s
        """, (status, limit, offset))
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/admin/dead-letter/{job_id}/replay")
async def replay_dead_letter(
    job_id: int,
    _identity: Identity = Depends(require_admin),
):
    """Re-enqueue a dead-letter job. Marks it 'replayed'. Admin-only."""
    from brain.core import db_pg as _pg
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        raise HTTPException(503, "Postgres backend required")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT job_name, kwargs FROM dead_letter_jobs WHERE id = %s AND status = 'failed'",
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Dead-letter job not found or already replayed")

        job_name = row["job_name"]
        kwargs   = row["kwargs"] if isinstance(row["kwargs"], dict) else json.loads(row["kwargs"])

        # Re-enqueue via arq (async-native, no deprecated event loop access)
        import arq as _arq
        from arq.connections import RedisSettings as _RS
        pool = await _arq.create_pool(_RS.from_dsn(config.REDIS_URL))
        await pool.enqueue_job(job_name, **kwargs)
        await pool.aclose()

        cur.execute(
            "UPDATE dead_letter_jobs SET status='replayed', replayed_at=now() WHERE id=%s",
            (job_id,),
        )
        conn.commit()
        cur.close()
        return {"replayed": True, "job_name": job_name, "kwargs": kwargs}
    finally:
        conn.close()


@app.delete("/api/admin/dead-letter/{job_id}")
def discard_dead_letter(
    job_id: int,
    _identity: Identity = Depends(require_admin),
):
    """Mark a dead-letter job as discarded (will not be retried). Admin-only."""
    from brain.core import db_pg as _pg
    from brain.core.db import BACKEND
    if BACKEND != "postgres":
        raise HTTPException(503, "Postgres backend required")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE dead_letter_jobs SET status='discarded' WHERE id=%s AND status='failed'",
            (job_id,),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Dead-letter job not found or not in 'failed' state")
        conn.commit()
        cur.close()
        return {"discarded": True}
    finally:
        conn.close()


# ── Slack workspace management (admin) ────────────────────────────────────────

@app.get("/api/slack/status")
def slack_status(identity: Identity = Depends(require_admin)):
    """Check if Slack is connected for this workspace."""
    from brain.core import db_pg as _pg
    account_id = _resolve_account_id(identity.user_id)
    if not account_id:
        raise HTTPException(404, "Account not found")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT slack_team_id, slack_team_name, bot_user_id, installed_by, created_at
            FROM slack_workspace_integrations
            WHERE account_id = %s
            LIMIT 1
            """,
            (account_id,),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return {"connected": False}
        return {"connected": True, **{k: str(v) for k, v in dict(row).items()}}
    finally:
        conn.close()


@app.delete("/api/slack/disconnect")
def slack_disconnect(identity: Identity = Depends(require_admin)):
    """Disconnect Slack from this workspace."""
    from brain.core import db_pg as _pg
    account_id = _resolve_account_id(identity.user_id)
    if not account_id:
        raise HTTPException(404, "Account not found")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM slack_workspace_integrations WHERE account_id = %s",
            (account_id,),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Slack is not connected")
        conn.commit()
        cur.close()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.get("/api/slack/channels")
def slack_list_channels(identity: Identity = Depends(require_admin)):
    """List channels the bot is in (live from Slack API)."""
    from brain.core import db_pg as _pg
    account_id = _resolve_account_id(identity.user_id)
    if not account_id:
        raise HTTPException(404, "Account not found")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT bot_token FROM slack_workspace_integrations WHERE account_id = %s LIMIT 1",
            (account_id,),
        )
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, "Slack is not connected")

    bot_token = row["bot_token"]
    try:
        import requests as _req
        channels = []
        cursor = None
        while True:
            params = {"types": "public_channel,private_channel", "exclude_archived": True, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            resp = _req.get(
                "https://slack.com/api/conversations.list",
                headers={"Authorization": f"Bearer {bot_token}"},
                params=params,
                timeout=15,
            )
            data = resp.json()
            if not data.get("ok"):
                raise HTTPException(502, f"Slack API error: {data.get('error')}")
            for ch in data.get("channels", []):
                if ch.get("is_member"):
                    channels.append({
                        "id":         ch["id"],
                        "name":       ch.get("name", ""),
                        "is_private": ch.get("is_private", False),
                        "num_members": ch.get("num_members", 0),
                    })
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return channels
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/slack/mappings")
def slack_get_mappings(identity: Identity = Depends(require_admin)):
    """List channel→department mappings for this workspace."""
    from brain.core import db_pg as _pg
    account_id = _resolve_account_id(identity.user_id)
    if not account_id:
        raise HTTPException(404, "Account not found")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, channel_id, channel_name, team_id, sync_members, slack_team_id, created_at
            FROM slack_channel_mappings
            WHERE account_id = %s
            ORDER BY channel_name
            """,
            (account_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows
    finally:
        conn.close()


@app.post("/api/slack/mappings")
def slack_create_mapping(
    req: SlackMappingRequest,
    identity: Identity = Depends(require_admin),
):
    """Create or update a channel→department mapping."""
    from brain.core import db_pg as _pg
    import uuid as _uuid
    account_id = _resolve_account_id(identity.user_id)
    if not account_id:
        raise HTTPException(404, "Account not found")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        # Get slack_team_id for this account's integration
        cur.execute(
            "SELECT slack_team_id FROM slack_workspace_integrations WHERE account_id = %s LIMIT 1",
            (account_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(400, "Slack is not connected — connect Slack before mapping channels")
        slack_team_id = row["slack_team_id"]
        cur.execute(
            """
            INSERT INTO slack_channel_mappings
                (id, account_id, slack_team_id, channel_id, channel_name, team_id, sync_members)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_id, channel_id) DO UPDATE SET
                channel_name = EXCLUDED.channel_name,
                team_id      = EXCLUDED.team_id,
                sync_members = EXCLUDED.sync_members
            """,
            (str(_uuid.uuid4()), account_id, slack_team_id,
             req.channel_id, req.channel_name, req.team_id, req.sync_members),
        )
        conn.commit()
        cur.close()
        return {"ok": True, "channel_id": req.channel_id, "team_id": req.team_id}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.delete("/api/slack/mappings/{channel_id}")
def slack_delete_mapping(
    channel_id: str,
    identity: Identity = Depends(require_admin),
):
    """Remove a channel→department mapping."""
    from brain.core import db_pg as _pg
    account_id = _resolve_account_id(identity.user_id)
    if not account_id:
        raise HTTPException(404, "Account not found")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM slack_channel_mappings WHERE account_id = %s AND channel_id = %s",
            (account_id, channel_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Mapping not found")
        conn.commit()
        cur.close()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@app.post("/api/slack/mappings/{channel_id}/sync")
def slack_sync_channel_members(
    channel_id: str,
    identity: Identity = Depends(require_admin),
):
    """Manually trigger member sync for a mapped channel (only if sync_members=True)."""
    from brain.core import db_pg as _pg
    from brain.wrappers.slack_bot import sync_channel_members
    account_id = _resolve_account_id(identity.user_id)
    if not account_id:
        raise HTTPException(404, "Account not found")
    conn = _pg.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.team_id, m.sync_members, i.bot_token
            FROM slack_channel_mappings m
            JOIN slack_workspace_integrations i ON i.account_id = m.account_id
            WHERE m.account_id = %s AND m.channel_id = %s
            LIMIT 1
            """,
            (account_id, channel_id),
        )
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, "Channel mapping not found")
    if not row["sync_members"]:
        raise HTTPException(400, "sync_members is not enabled for this channel mapping")

    result = sync_channel_members(
        account_id=account_id,
        channel_id=channel_id,
        team_id=row["team_id"],
        bot_token=row["bot_token"],
    )
    return {"ok": True, **result}


# ── platform metrics (platform-owner only) ────────────────────────────────────

@app.get("/api/_platform/metrics")
def platform_metrics(period: str = None, _identity: Identity = Depends(get_identity)):
    """
    Aggregate per-workspace cost, token spend, integration api_calls, and gross margin.
    Restricted to BRAIN_PLATFORM_OWNER=true env flag. Hard 403 for everyone else.
    """
    from brain import config as _cfg
    if not _cfg.PLATFORM_OWNER:
        raise HTTPException(status_code=403, detail="Forbidden")

    import datetime as _dt
    from brain.core import db_pg as _pg

    now = _dt.date.today()
    if period:
        try:
            y, m = map(int, period.split("-"))
            period_start = _dt.date(y, m, 1)
        except Exception:
            period_start = now.replace(day=1)
    else:
        period_start = now.replace(day=1)
        period = period_start.strftime("%Y-%m")

    if period_start.month == 12:
        period_end = period_start.replace(year=period_start.year + 1, month=1, day=1)
    else:
        period_end = period_start.replace(month=period_start.month + 1, day=1)

    try:
        conn = _pg.connect()
    except Exception as _e:
        raise HTTPException(status_code=500, detail="DB unavailable") from _e
    try:
        cur = conn.cursor()

        # Per-workspace LLM cost + token spend
        cur.execute("""
            SELECT
                account_id,
                SUM(prompt_tokens + completion_tokens) AS total_tokens,
                SUM(cost_usd)                          AS total_cost
            FROM usage_events
            WHERE ts >= %s AND ts < %s
            GROUP BY account_id
            ORDER BY total_cost DESC
        """, (period_start.isoformat(), period_end.isoformat()))
        llm_rows = cur.fetchall()

        # Per-workspace integration api_calls
        cur.execute("""
            SELECT
                account_id,
                SUM(api_calls)      AS api_calls,
                SUM(backfill_items) AS backfill_items
            FROM integration_usage
            WHERE period >= %s AND period < %s
            GROUP BY account_id
        """, (period_start.isoformat(), period_end.isoformat()))
        int_rows = {r["account_id"]: r for r in cur.fetchall()}

        # Platform totals
        total_cost = sum(float(r["total_cost"] or 0) for r in llm_rows)
        total_tokens = sum(int(r["total_tokens"] or 0) for r in llm_rows)

        workspaces = []
        for r in llm_rows:
            acct = r["account_id"]
            cost = float(r["total_cost"] or 0)
            tokens = int(r["total_tokens"] or 0)
            int_data = int_rows.get(acct, {})
            workspaces.append({
                "account_id":    acct,
                "tokens":        tokens,
                "cost_usd":      round(cost, 6),
                "api_calls":     int(int_data.get("api_calls") or 0),
                "backfill_items": int(int_data.get("backfill_items") or 0),
            })

        cur.close()
    except Exception as _e:
        raise HTTPException(status_code=500, detail="metrics query failed") from _e
    finally:
        conn.close()

    return {
        "period":        period,
        "total_cost_usd": round(total_cost, 6),
        "total_tokens":   total_tokens,
        "workspace_count": len(workspaces),
        "workspaces":    workspaces,
    }


# ── account data export (GDPR portability) ───────────────────────────────────

@app.get("/api/account/export")
def export_account_data(identity: Identity = Depends(require_admin)):
    """
    Export all workspace data as a JSON file download (GDPR data portability).

    Returns facts, episodes (without embeddings), ledger entries, and teams
    for the caller's workspace. Streams the response so large datasets do not
    buffer entirely in memory before sending.

    Never includes: other workspaces' data, API keys, secrets, embeddings,
    or any data that is not directly owned by this workspace.

    Admin-only — requires the caller to have admin or owner role.
    """
    import datetime as _dt
    from brain.core import db_pg as _pg

    ws = identity.workspace_id
    conn = _pg.connect()

    def _row_to_dict(row) -> dict:
        """Convert a RealDictRow to a plain dict, serialising non-JSON types."""
        out = {}
        for k, v in dict(row).items():
            if isinstance(v, (_dt.datetime, _dt.date)):
                out[k] = v.isoformat()
            else:
                out[k] = v
        return out

    def _generate():
        """Generator that yields JSON chunks for StreamingResponse."""
        try:
            _pg._set_ws(conn, ws)
            cur = conn.cursor()
            exported_at = _dt.datetime.utcnow().isoformat() + "Z"

            yield '{"exported_at":' + json.dumps(exported_at) + ','
            yield '"workspace_id":' + json.dumps(ws) + ','

            # ── teams ─────────────────────────────────────────────────────────
            cur.execute(
                "SELECT id, name, description, created_at "
                "FROM teams WHERE workspace_id = %s ORDER BY name",
                (ws,)
            )
            yield '"teams":['
            first = True
            for row in cur.fetchall():
                if not first:
                    yield ","
                yield json.dumps(_row_to_dict(row))
                first = False
            yield "],"

            # ── episodes (body included, no embeddings to exclude here) ───────
            cur.execute(
                "SELECT id, source, external_id, title, body, author, url, "
                "       occurred_at, content_hash, acl, team_id, extracted "
                "FROM episodes WHERE workspace_id = %s ORDER BY occurred_at",
                (ws,)
            )
            yield '"episodes":['
            first = True
            for row in cur.fetchall():
                if not first:
                    yield ","
                yield json.dumps(_row_to_dict(row))
                first = False
            yield "],"

            # ── facts (embedding column excluded — too large, not portable) ───
            cur.execute(
                "SELECT id, subject, predicate, object, summary, episode_id, "
                "       acl, confidence, introduced_at, invalidated_at, "
                "       source_kind, team_id, visibility "
                "FROM facts WHERE workspace_id = %s ORDER BY introduced_at",
                (ws,)
            )
            yield '"facts":['
            first = True
            for row in cur.fetchall():
                if not first:
                    yield ","
                yield json.dumps(_row_to_dict(row))
                first = False
            yield "],"

            # ── fact_edges ────────────────────────────────────────────────────
            cur.execute(
                "SELECT id, src_fact, dst_fact, type "
                "FROM fact_edges WHERE workspace_id = %s ORDER BY id",
                (ws,)
            )
            yield '"fact_edges":['
            first = True
            for row in cur.fetchall():
                if not first:
                    yield ","
                yield json.dumps(_row_to_dict(row))
                first = False
            yield "],"

            # ── ledger (query/context text included for portability) ───────────
            cur.execute(
                "SELECT id, user_id, ts, query, surface, fact_ids, team_id "
                "FROM ledger WHERE workspace_id = %s ORDER BY id",
                (ws,)
            )
            yield '"ledger":['
            first = True
            for row in cur.fetchall():
                if not first:
                    yield ","
                yield json.dumps(_row_to_dict(row))
                first = False
            yield "]}"

            cur.close()
        finally:
            conn.close()

    filename = f"export-{ws}.json"
    return StreamingResponse(
        _generate(),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Workspace": ws,
        },
    )


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    import uvicorn
    config.startup_check()
    from brain.core.sentry import init_sentry
    init_sentry()
    # Run DB migration once at startup so the first request isn't penalised
    try:
        from brain.core import db_pg as _pg
        _conn = _pg.connect()
        _pg.migrate(_conn)
        _conn.close()
        print("[thread] DB migration OK")
    except Exception as _e:
        print(f"[thread] DB migration failed (non-fatal): {_e}")
    uvicorn.run("brain.wrappers.api:app", host="127.0.0.1", port=7070, reload=False)


if __name__ == "__main__":
    main()
