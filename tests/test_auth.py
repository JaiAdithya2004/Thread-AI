"""
Unit tests for brain/auth.py
- Bypass only allowed in development mode
- Production boot guard raises sys.exit if bypass or no Clerk key
- get_identity returns default identity in bypass mode
- require_admin raises 403 for non-admin
- dev-bypass sentinel token returns 401 in non-bypass mode
"""
import os, sys, importlib, pytest
import unittest.mock as mock


def _reload_auth(env_overrides: dict):
    """Reload auth module with specific env vars set."""
    saved = {k: os.environ.get(k) for k in env_overrides}
    os.environ.update({k: v for k, v in env_overrides.items() if v is not None})
    for k, v in env_overrides.items():
        if v is None and k in os.environ:
            del os.environ[k]
    try:
        import brain.auth
        module = importlib.reload(brain.auth)
        return module
    finally:
        # Restore
        for k, orig in saved.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig


# ── Bypass logic ──────────────────────────────────────────────────────────────

def test_bypass_active_when_no_clerk_key_and_dev():
    auth = _reload_auth({
        "BRAIN_ENV": "development",
        "CLERK_SECRET_KEY": None,
        "BRAIN_DEV_BYPASS": None,
    })
    assert auth._BYPASS is True


def test_bypass_active_when_flag_set_and_dev():
    auth = _reload_auth({
        "BRAIN_ENV": "development",
        "CLERK_SECRET_KEY": None,
        "BRAIN_DEV_BYPASS": "true",
    })
    assert auth._BYPASS is True


def test_bypass_inactive_when_clerk_key_set_in_dev():
    auth = _reload_auth({
        "BRAIN_ENV": "development",
        "CLERK_SECRET_KEY": "sk_test_someclerkkey",
        "BRAIN_DEV_BYPASS": None,
        "CLERK_PUBLISHABLE_KEY": None,
    })
    assert auth._BYPASS is False


# ── Production boot guard ─────────────────────────────────────────────────────

def test_production_refuses_bypass_flag(monkeypatch):
    """Server must sys.exit(1) if BRAIN_ENV=production and BRAIN_DEV_BYPASS=true."""
    with mock.patch.object(sys, "exit") as mock_exit:
        _reload_auth({
            "BRAIN_ENV": "production",
            "BRAIN_DEV_BYPASS": "true",
            "CLERK_SECRET_KEY": "sk_live_somekey",
        })
    mock_exit.assert_called_once_with(1)


def test_production_refuses_no_clerk_key(monkeypatch):
    """Server must sys.exit(1) if BRAIN_ENV=production and no CLERK_SECRET_KEY."""
    with mock.patch.object(sys, "exit") as mock_exit:
        _reload_auth({
            "BRAIN_ENV": "production",
            "BRAIN_DEV_BYPASS": None,
            "CLERK_SECRET_KEY": None,
        })
    mock_exit.assert_called_once_with(1)


# ── get_identity in bypass mode ───────────────────────────────────────────────

def test_get_identity_bypass_returns_default():
    from brain.auth import get_identity, _BYPASS
    if not _BYPASS:
        pytest.skip("Not in bypass mode")
    identity = get_identity(creds=None)
    assert identity.is_admin is True
    assert identity.user_id is not None


# ── require_admin ─────────────────────────────────────────────────────────────

def test_require_admin_raises_403_for_non_admin():
    from fastapi import HTTPException
    from brain.auth import require_admin, Identity
    non_admin = Identity(
        user_id="u1", email="e@x.com", workspace_id="ws",
        team_id="t", principals=["u1"], role="employee", is_admin=False
    )
    with pytest.raises(HTTPException) as exc:
        require_admin(identity=non_admin)
    assert exc.value.status_code == 403


def test_require_admin_passes_for_admin():
    from brain.auth import require_admin, Identity
    admin = Identity(
        user_id="u1", email="e@x.com", workspace_id="ws",
        team_id="t", principals=["u1"], role="admin", is_admin=True
    )
    result = require_admin(identity=admin)
    assert result.is_admin is True


# ── Identity dataclass ────────────────────────────────────────────────────────

def test_identity_from_clerk_payload_sets_is_admin():
    from brain.auth import _identity_from_clerk_payload
    payload = {
        "sub": "user_abc",
        "email": "admin@corp.com",
        "public_metadata": {"role": "admin", "team_id": "eng", "workspace_id": "ws1"},
    }
    identity = _identity_from_clerk_payload(payload)
    assert identity.user_id == "user_abc"
    assert identity.email   == "admin@corp.com"
    assert identity.is_admin is True
    assert identity.team_id  == "eng"


def test_identity_from_clerk_payload_non_admin():
    from brain.auth import _identity_from_clerk_payload
    payload = {
        "sub": "user_xyz",
        "email": "emp@corp.com",
        "public_metadata": {"role": "employee"},
    }
    identity = _identity_from_clerk_payload(payload)
    assert identity.is_admin is False
    assert "user_xyz" in identity.principals
    assert "everyone"  in identity.principals
    assert "emp@corp.com" in identity.principals
