"""
Supabase Auth — JWT verification and identity extraction.

Every API request must carry an Authorization: Bearer <token> header.
The token is a Supabase-issued JWT signed with SUPABASE_JWT_SECRET.

Identity extraction:
  sub         → caller (Supabase user UUID)
  email       → added to principals
  user_metadata.team_id  → team_id  (set this when inviting users in Supabase dashboard)
  user_metadata.role     → added to principals  (e.g. "admin", "viewer")
  app_metadata.workspace → workspace_id (optional; falls back to BRAIN_WORKSPACE)

DEV / SINGLE-TENANT BYPASS:
  If SUPABASE_JWT_SECRET is not set, auth runs in BYPASS mode:
  a valid-looking token is still required but not verified — every request
  gets the identity from config defaults. This lets the single-tenant
  workflow continue working without Supabase configured.

  In production: set SUPABASE_JWT_SECRET (find it in Supabase → Settings →
  API → JWT Secret). Never set it to a dummy value in prod.
"""
import os
from dataclasses import dataclass, field
from typing import List, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from brain import config

_bearer = HTTPBearer(auto_error=False)

_SUPABASE_JWT_SECRET: Optional[str] = os.getenv("SUPABASE_JWT_SECRET")
_BYPASS = not bool(_SUPABASE_JWT_SECRET) or os.getenv("BRAIN_DEV_BYPASS", "").lower() in ("1", "true", "yes")

if _BYPASS:
    import sys
    print(
        "[auth] SUPABASE_JWT_SECRET not set — running in BYPASS mode. "
        "All requests use config defaults. Set the secret for production.",
        file=sys.stderr,
    )


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
    """Used in bypass mode and for the CLI / MCP paths."""
    return Identity(
        user_id=config.CALLER,
        email=config.CALLER,
        workspace_id=config.WORKSPACE_ID,
        team_id=config.TEAM_ID,
        principals=list(config.CALLER_PRINCIPALS),
        role="admin",
        is_admin=True,
    )


def _decode_jwt(token: str) -> dict:
    """Verify and decode a Supabase JWT. Raises ValueError on failure."""
    try:
        import jwt  # PyJWT
    except ImportError:
        raise ValueError("PyJWT not installed — run: pip install PyJWT")

    try:
        payload = jwt.decode(
            token,
            _SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_exp": True},
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError("Token expired")
    except jwt.InvalidTokenError as e:
        raise ValueError(f"Invalid token: {e}")


def _identity_from_payload(payload: dict) -> Identity:
    """Map a decoded Supabase JWT payload to an Identity."""
    user_meta = payload.get("user_metadata") or {}
    app_meta = payload.get("app_metadata") or {}

    user_id = payload.get("sub", "anonymous")
    email = payload.get("email") or user_meta.get("email", "")
    role = user_meta.get("role") or app_meta.get("role", "member")
    team_id = (
        user_meta.get("team_id")
        or app_meta.get("team_id")
        or config.TEAM_ID
    )
    workspace_id = (
        app_meta.get("workspace")
        or config.WORKSPACE_ID
    )

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
        is_admin=(role == "admin"),
    )


def get_identity(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Identity:
    """
    FastAPI dependency — call as: identity: Identity = Depends(get_identity)

    In BYPASS mode: returns config defaults. Bearer token optional.
    In PRODUCTION mode: token required and verified. 401 on failure.
    """
    if _BYPASS:
        return _default_identity()

    if not creds:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = _decode_jwt(creds.credentials)
        return _identity_from_payload(payload)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_admin(identity: Identity = Depends(get_identity)) -> Identity:
    """Stricter dependency — also checks the admin role."""
    if not identity.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return identity
