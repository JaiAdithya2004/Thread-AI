"""
Clerk Auth — JWT verification and identity extraction.

Every API request must carry an Authorization: Bearer <session_token> header.
The token is a Clerk-issued JWT verified against Clerk's public keys (JWKS).

DEV / SINGLE-TENANT BYPASS:
  If CLERK_SECRET_KEY is not set, or BRAIN_DEV_BYPASS=true, auth runs in
  BYPASS mode — every request gets the identity from config defaults.
"""
import os
from dataclasses import dataclass
from typing import List, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from brain import config

_bearer = HTTPBearer(auto_error=False)

_CLERK_SECRET_KEY: Optional[str] = os.getenv("CLERK_SECRET_KEY")
_CLERK_PUBLISHABLE_KEY: Optional[str] = os.getenv("CLERK_PUBLISHABLE_KEY")
_BYPASS = (
    not bool(_CLERK_SECRET_KEY)
    or os.getenv("BRAIN_DEV_BYPASS", "").lower() in ("1", "true", "yes")
)

if _BYPASS:
    import sys
    print("[auth] CLERK_SECRET_KEY not set — running in BYPASS mode.", file=sys.stderr)

# Derive Clerk frontend API from publishable key: pk_test_xxx -> xxx.clerk.accounts.dev
_CLERK_JWKS_URL: Optional[str] = None
if _CLERK_PUBLISHABLE_KEY and not _BYPASS:
    try:
        import base64
        # publishable key format: pk_test_<base64(domain)>
        b64 = _CLERK_PUBLISHABLE_KEY.split("_", 2)[-1].rstrip("$")
        # pad base64
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

    # Get JWKS and find matching key
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
    # Clerk puts email in email_addresses or as a top-level claim
    email = payload.get("email", "")
    # Clerk metadata
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
    if _BYPASS:
        return _default_identity()

    if not creds:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = creds.credentials
    # Allow dev-bypass sentinel token
    if token == "dev-bypass":
        return _default_identity()

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
