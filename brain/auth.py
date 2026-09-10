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


# Workspace that guest tokens are locked to — must match seed_demo.py DEMO_WS.
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


def _identity_from_clerk_payload(payload: dict) -> Identity:
    user_id = payload.get("sub", "anonymous")
    email = payload.get("email", "")
    public_meta = payload.get("public_metadata") or {}
    unsafe_meta = payload.get("unsafe_metadata") or {}

    role = public_meta.get("role") or unsafe_meta.get("role", "member")
    team_id = public_meta.get("team_id") or unsafe_meta.get("team_id") or config.TEAM_ID
    workspace_id = public_meta.get("workspace_id") or config.WORKSPACE_ID

    is_guest = (role == "guest")
    # Guests are always locked to the demo workspace, regardless of what the
    # JWT says — this is the server-side enforcement, not just a UI hint.
    if is_guest:
        workspace_id = GUEST_WORKSPACE

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


def _derive_guest_secret() -> Optional[str]:
    """Signing key for guest demo tokens.

    Prefer an explicit GUEST_TOKEN_SECRET. Failing that, derive one from the
    key-encryption key rather than disabling demo mode: the secret only signs
    4-hour tokens scoped to the read-only demo workspace, so a separate variable
    is a step that gets skipped, and skipping it turns "Try live demo" into a
    503 with nothing to explain it. Deriving through HKDF keeps the two keys
    independent -- the guest secret never reveals the encryption key.
    """
    explicit = _env("GUEST_TOKEN_SECRET")
    if explicit:
        return explicit
    root = _env("BRAIN_KEY_ENCRYPTION_KEY")
    if not root:
        return None
    import hashlib
    return hashlib.blake2b(
        root.encode(), digest_size=32, person=b"thread-guest-tok"
    ).hexdigest()


_GUEST_TOKEN_SECRET: Optional[str] = _derive_guest_secret()

# Prefix that identifies a guest self-signed token (never clashes with Clerk JWTs)
_GUEST_TOKEN_PREFIX = "guest."


def _decode_guest_token(token: str) -> Identity:
    """Decode and verify a guest token issued by /api/guest-session."""
    if not _GUEST_TOKEN_SECRET:
        raise ValueError("Guest sessions are not configured on this server.")
    import hmac
    import hashlib
    import base64
    import json as _json

    # Format: "guest.<b64-payload>.<hex-sig>"
    try:
        _, b64, sig = token.split(".", 2)
    except ValueError:
        raise ValueError("Malformed guest token")

    payload_bytes = base64.urlsafe_b64decode(b64 + "==")
    expected = hmac.new(
        _GUEST_TOKEN_SECRET.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, sig):
        raise ValueError("Guest token signature invalid")

    payload = _json.loads(payload_bytes)

    import time as _time
    if payload.get("exp", 0) < _time.time():
        raise ValueError("Guest session has expired — click 'Try live demo' again.")

    return Identity(
        user_id=payload["sub"],
        email="guest@demo",
        workspace_id=GUEST_WORKSPACE,
        team_id=payload.get("team_id", ""),
        principals=["everyone"],
        role="guest",
        is_admin=False,
        is_guest=True,
    )


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

    # Guest session token — self-signed, no Clerk involved
    if token.startswith(_GUEST_TOKEN_PREFIX):
        try:
            return _decode_guest_token(token)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(e),
                headers={"WWW-Authenticate": "Bearer"},
            )

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
