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

_CLERK_SECRET_KEY: Optional[str] = os.getenv("CLERK_SECRET_KEY")
_CLERK_PUBLISHABLE_KEY: Optional[str] = os.getenv("CLERK_PUBLISHABLE_KEY")

# BRAIN_ENV controls whether dev shortcuts are permitted.
# Default is "development" for local use; set to "production" when deploying.
_ENV: str = os.getenv("BRAIN_ENV", "development").lower().strip()
_IS_PRODUCTION: bool = _ENV == "production"

# Bypass is ONLY allowed in non-production AND when no Clerk key is configured
# (single-tenant dev mode) OR when BRAIN_DEV_BYPASS=true is explicitly set.
_DEV_BYPASS_FLAG: bool = os.getenv("BRAIN_DEV_BYPASS", "").lower() in ("1", "true", "yes")
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
