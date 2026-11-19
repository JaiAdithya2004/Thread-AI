"""
Clerk Auth — JWT verification and identity extraction.

Every API request must carry an Authorization: Bearer <session_token> header.
The token is a Clerk-issued JWT verified against Clerk's public keys (JWKS).

DEV BYPASS:
  Only active when BRAIN_ENV=development AND BRAIN_DEV_BYPASS=true.
  The server REFUSES to boot in production (BRAIN_ENV=production) if bypass
  is enabled — see the startup check at the bottom of this module.
"""
import os
import sys
from dataclasses import dataclass
from typing import List, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from brain import config

_bearer = HTTPBearer(auto_error=False)

def _env(name: str, default: str = "") -> str:
    """Read an env var, tolerating quotes and stray whitespace.

    python-dotenv strips surrounding quotes from a .env file, but a hosting
    dashboard does not: a value pasted as "pk_test_..." arrives with the quote
    characters still in it. That silently breaks any parse of the value, so
    strip them here rather than in each caller.
    """
    return (os.getenv(name) or default).strip().strip('"').strip("'").strip()


_CLERK_SECRET_KEY: Optional[str] = _env("CLERK_SECRET_KEY") or None
_CLERK_PUBLISHABLE_KEY: Optional[str] = _env("CLERK_PUBLISHABLE_KEY") or None

# BRAIN_ENV controls whether dev shortcuts are permitted.
# Default is "development" for local use; set to "production" when deploying.
#
# A hosted deployment is never "development", whatever BRAIN_ENV says. Scoping
# BRAIN_ENV to Production only -- easy to do in a dashboard -- would otherwise
# leave a Preview build defaulting to development, and a Preview build that also
# lacked a Clerk key would serve the whole app unauthenticated. Treat any
# recognised hosting environment as production unless BRAIN_ENV explicitly
# overrides it, so the unsafe mode requires a deliberate statement.
_HOSTED: bool = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
_ENV: str = _env("BRAIN_ENV", "production" if _HOSTED else "development").lower()
_IS_PRODUCTION: bool = _ENV == "production"

# Bypass is ONLY allowed in non-production AND when no Clerk key is configured
# (single-tenant dev mode) OR when BRAIN_DEV_BYPASS=true is explicitly set.
_DEV_BYPASS_FLAG: bool = _env("BRAIN_DEV_BYPASS").lower() in ("1", "true", "yes")
_BYPASS: bool = (
    not _IS_PRODUCTION
    and (not bool(_CLERK_SECRET_KEY) or _DEV_BYPASS_FLAG)
)

# ── Production safety check ───────────────────────────────────────────────────
# This runs at import time (server startup). If someone accidentally sets
# BRAIN_DEV_BYPASS=true in a production environment, we refuse to start.
if _IS_PRODUCTION and _DEV_BYPASS_FLAG:
    print(
        "[auth] FATAL: BRAIN_DEV_BYPASS=true is set but BRAIN_ENV=production. "
        "The dev-bypass path is not allowed in production. "
        "Unset BRAIN_DEV_BYPASS or change BRAIN_ENV before starting the server.",
        file=sys.stderr,
    )
    sys.exit(1)

if _IS_PRODUCTION and not _CLERK_SECRET_KEY:
    print(
        "[auth] FATAL: BRAIN_ENV=production but CLERK_SECRET_KEY is not set. "
        "Authentication cannot run without a Clerk secret key in production.",
        file=sys.stderr,
    )
    sys.exit(1)

if _BYPASS:
    print(
        "[auth] DEV MODE — auth bypass active (BRAIN_ENV=development, no Clerk key). "
        "Set BRAIN_ENV=production and CLERK_SECRET_KEY to enforce authentication.",
        file=sys.stderr,
    )


# Derive Clerk JWKS URL from publishable key: pk_test_xxx -> domain.clerk.accounts.dev
_CLERK_JWKS_URL: Optional[str] = None
if _CLERK_PUBLISHABLE_KEY and not _BYPASS:
    try:
        import base64
        b64 = _CLERK_PUBLISHABLE_KEY.split("_", 2)[-1].rstrip("$")
        b64 += "=" * (-len(b64) % 4)
        domain = base64.b64decode(b64).decode("utf-8").rstrip("$")
        _CLERK_JWKS_URL = f"https://{domain}/.well-known/jwks.json"
    except Exception:
        _CLERK_JWKS_URL = None


@dataclass
class Identity:
    user_id: str
    email: str
    workspace_id: str
    team_id: str
    principals: List[str]
    role: str = "member"
    is_admin: bool = False
    is_guest: bool = False   # guests may only read from the demo workspace


# Reserved: a signed-in user is never placed in this workspace (see resolve_workspace).
# There is no demo or guest mode any more; the name is kept so that nothing can
# be mapped onto an old demo workspace that still exists in a database.
GUEST_WORKSPACE = "ws_demo"


def _default_identity() -> Identity:
    return Identity(
        user_id=config.CALLER,
        email=config.CALLER,
        workspace_id=config.WORKSPACE_ID,
        team_id=config.TEAM_ID,
        principals=list(config.CALLER_PRINCIPALS),
        role="admin",
        is_admin=True,
    )


_jwks_cache: Optional[dict] = None

def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache:
        return _jwks_cache
    import requests
    resp = requests.get(_CLERK_JWKS_URL, timeout=10)
    resp.raise_for_status()
    _jwks_cache = resp.json()
    return _jwks_cache


def _decode_clerk_jwt(token: str) -> dict:
    try:
        import jwt as pyjwt
    except ImportError:
        raise ValueError("PyJWT not installed")

    jwks = _get_jwks()
    headers = pyjwt.get_unverified_header(token)
    kid = headers.get("kid")

    public_key = None
    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            public_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(key_data)
            break

    if not public_key:
        raise ValueError(f"No matching public key found for kid={kid}")

    payload = pyjwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        options={"verify_exp": True},
    )
    return payload


# ── Workspace assignment ─────────────────────────────────────────────────────
# A signed-in user's workspace comes from their account, resolved on the server.
# It used to fall back to one deployment-wide default, which put every new
# sign-up into the same workspace (in production, the public demo's) where the
# demo guard then blocked them from doing anything.

_WS_CACHE: dict = {}          # user_id -> (workspace_id, expires_at)
_WS_CACHE_TTL = 300


def _fallback_workspace(user_id: str) -> str:
    """A private workspace for a user who has no account yet (or whose account
    cannot be read). Derived from the user id, so it is never shared and never the
    demo. Not cached: the real account workspace takes over as soon as it exists."""
    import hashlib
    return "ws_u_" + hashlib.sha256(user_id.encode()).hexdigest()[:16]


def _lookup_account_workspace(user_id: str) -> Optional[str]:
    from brain.core import db_pg
    return db_pg.workspace_for_user(user_id)


def resolve_workspace(user_id: str, claimed: Optional[str] = None) -> str:
    """Workspace for a non-guest user. Never the demo workspace."""
    import time as _time
    ws = claimed
    if not ws:
        hit = _WS_CACHE.get(user_id)
        if hit and hit[1] > _time.time():
            ws = hit[0]
        else:
            try:
                ws = _lookup_account_workspace(user_id)
            except Exception:
                ws = None
            if ws:
                _WS_CACHE[user_id] = (ws, _time.time() + _WS_CACHE_TTL)
    if not ws or ws == GUEST_WORKSPACE:
        ws = _fallback_workspace(user_id)
    return ws


_ROLE_CACHE: dict = {}        # user_id -> (role, expires_at)


def _lookup_membership_role(user_id: str) -> Optional[str]:
    from brain.core import db_pg
    return db_pg.membership_role_for_user(user_id)


def resolve_role(user_id: str, claimed: Optional[str] = None) -> str:
    """The caller's role. A role set on the Clerk user wins (an operator can still
    promote or demote by hand); otherwise it comes from the account membership
    the server wrote at sign-up or invitation. Without this, every new account
    owner was a plain "member" and could not sync, back-fill or manage anything
    in their own workspace."""
    import time as _time
    if claimed:
        return claimed
    hit = _ROLE_CACHE.get(user_id)
    if hit and hit[1] > _time.time():
        role = hit[0]
    else:
        try:
            role = _lookup_membership_role(user_id)
        except Exception:
            role = None
        if role:
            _ROLE_CACHE[user_id] = (role, _time.time() + _WS_CACHE_TTL)
    return role if role in ("owner", "admin") else "member"


def _identity_from_clerk_payload(payload: dict) -> Identity:
    user_id = payload.get("sub", "anonymous")
    email = payload.get("email", "")
    # Only `public_metadata` is trusted: it can be written by the backend alone.
    # `unsafe_metadata` is editable by the signed-in user from the browser, so
    # reading a role, team or workspace from it would let anyone grant themselves
    # admin or read another team's facts.
    public_meta = payload.get("public_metadata") or {}

    role = resolve_role(user_id, public_meta.get("role"))
    team_id = public_meta.get("team_id") or config.TEAM_ID
    is_guest = (role == "guest")
    # Guests are always locked to the demo workspace, regardless of what the
    # JWT says — this is the server-side enforcement, not just a UI hint.
    # Everyone else gets the workspace of their own account, and never the demo.
    if is_guest:
        workspace_id = GUEST_WORKSPACE
    else:
        workspace_id = resolve_workspace(user_id, public_meta.get("workspace_id"))

    principals = list({user_id, "everyone"})
    if email:
        principals.append(email)
    if role:
        principals.append(role)

    return Identity(
        user_id=user_id,
        email=email,
        workspace_id=workspace_id,
        team_id=team_id,
        principals=principals,
        role=role,
        is_admin=(role in ("admin", "owner")),
        is_guest=is_guest,
    )


# ── Signed OAuth state ────────────────────────────────────────────────────────
# The integration callback is a browser redirect from a third party, so it
# carries no bearer token and cannot be authenticated the usual way. Instead the
# identity is sealed into a signed, expiring `state` value when the connection is
# started (by an authenticated call) and only trusted when it verifies on the
# way back. Reading user/workspace ids from the callback's query string, as it
# used to, let anyone write routing rows for any workspace.

def _state_secret() -> Optional[bytes]:
    import hashlib
    root = _env("BRAIN_KEY_ENCRYPTION_KEY") or _env("CLERK_SECRET_KEY")
    if not root:
        # Local development without any secret: a fixed key is acceptable there
        # and nowhere else.
        return None if _IS_PRODUCTION else b"thread-dev-oauth-state-key-not-secret"
    return hashlib.blake2b(root.encode(), digest_size=32, person=b"thread-oauth-st").digest()


def sign_state(payload: dict, ttl_seconds: int = 900) -> str:
    """Seal `payload` into a tamper-evident, expiring token."""
    import base64, hashlib, hmac, json as _json, time as _time
    key = _state_secret()
    if not key:
        raise RuntimeError("No signing secret configured for OAuth state.")
    body = dict(payload, exp=int(_time.time()) + ttl_seconds)
    raw = _json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(key, raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode() + "." + sig


def verify_state(token: str) -> dict:
    """Return the sealed payload, or raise ValueError if forged, malformed or expired."""
    import base64, hashlib, hmac, json as _json, time as _time
    key = _state_secret()
    if not key or not token or "." not in token:
        raise ValueError("invalid state")
    b64, sig = token.rsplit(".", 1)
    try:
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except Exception:
        raise ValueError("invalid state")
    expected = hmac.new(key, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("invalid state")
    body = _json.loads(raw)
    if body.get("exp", 0) < _time.time():
        raise ValueError("state expired")
    return body

def get_identity(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Identity:
    # Dev bypass — only reachable when _BYPASS is True (not production)
    if _BYPASS:
        return _default_identity()

    if not creds:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = creds.credentials

    # In production the "dev-bypass" sentinel token must never work.
    # _BYPASS is False here by definition (checked above), so this path
    # is already unreachable — but assert explicitly for clarity.
    if token == "dev-bypass":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Every non-dev request must carry a Clerk-issued token. There are no guest or
    # demo sessions: a token that is not a valid Clerk JWT is rejected below.
    try:
        payload = _decode_clerk_jwt(token)
        return _identity_from_clerk_payload(payload)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_admin(identity: Identity = Depends(get_identity)) -> Identity:
    if not identity.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return identity


def require_not_guest(identity: Identity = Depends(get_identity)) -> Identity:
    """Block guests from any mutating or sensitive endpoint.

    Guests (role='guest') are read-only viewers of the demo workspace.
    They may NOT: write facts, change settings, touch connectors/secrets,
    run backfills, invite/manage members, delete anything, or access any
    workspace other than ws_demo.

    This is a server-side enforcement — hiding it in the UI is not enough.
    """
    if identity.is_guest:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Guest accounts are read-only. This action is not permitted.",
        )
    return identity


def require_non_demo_workspace(identity: Identity = Depends(get_identity)) -> Identity:
    """Ensure the caller is NOT operating on the demo workspace.

    Guests are always locked to ws_demo, so this guard also implicitly blocks
    guests. Non-guest users who somehow land on ws_demo (e.g. a stale JWT) are
    also rejected so demo data is never mutated by accident.
    """
    if identity.workspace_id == GUEST_WORKSPACE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action is not permitted on the demo workspace.",
        )
    return identity
