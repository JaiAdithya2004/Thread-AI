"""
Unit tests for input validation (Pydantic models) and rate limiter.
"""
import pytest
from pydantic import ValidationError


# ── Request model validation ──────────────────────────────────────────────────

def _import():
    from brain.wrappers.api import (
        SearchRequest, RecordRequest, CorrectRequest,
        CreateTeamRequest, BackfillRequest, SyncRequest,
        ComposioConnectRequest, SignupRequest, InviteRequest,
        MemberPatchRequest, InviteAcceptRequest,
        ConversationRequest, MessageRequest,
    )
    return locals()


def test_search_request_valid():
    from brain.wrappers.api import SearchRequest
    r = SearchRequest(query="what is our pricing?")
    assert r.query == "what is our pricing?"


def test_search_request_cross_team_default_false():
    from brain.wrappers.api import SearchRequest
    r = SearchRequest(query="test")
    assert r.cross_team is False


def test_record_request_valid():
    from brain.wrappers.api import RecordRequest
    r = RecordRequest(text="We decided to use Postgres.")
    assert r.text == "We decided to use Postgres."
    assert r.shared is False


def test_create_team_request_valid():
    from brain.wrappers.api import CreateTeamRequest
    r = CreateTeamRequest(team_id="engineering", name="Engineering")
    assert r.team_id == "engineering"


def test_create_team_request_bad_slug():
    from brain.wrappers.api import CreateTeamRequest
    with pytest.raises((ValidationError, ValueError)):
        CreateTeamRequest(team_id="Engineering Dept!", name="Eng")


def test_create_team_request_name_too_long():
    from brain.wrappers.api import CreateTeamRequest
    with pytest.raises((ValidationError, ValueError)):
        CreateTeamRequest(team_id="eng", name="x" * 129)


def test_create_team_request_description_too_long():
    from brain.wrappers.api import CreateTeamRequest
    with pytest.raises((ValidationError, ValueError)):
        CreateTeamRequest(team_id="eng", name="Eng", description="x" * 501)


def test_backfill_request_valid():
    from brain.wrappers.api import BackfillRequest
    r = BackfillRequest(source="composio")
    assert r.source == "composio"


def test_backfill_request_invalid_slug():
    from brain.wrappers.api import BackfillRequest
    with pytest.raises((ValidationError, ValueError)):
        BackfillRequest(source="My Source!")


def test_signup_request_bad_account_type():
    from brain.wrappers.api import SignupRequest
    with pytest.raises((ValidationError, ValueError)):
        SignupRequest(email="a@b.com", password="pass", account_type="enterprise", name="Acme")


def test_signup_request_email_too_long():
    from brain.wrappers.api import SignupRequest
    with pytest.raises((ValidationError, ValueError)):
        SignupRequest(email="a" * 255 + "@b.com", password="pass",
                     account_type="individual", name="Bob")


def test_invite_request_bad_role():
    from brain.wrappers.api import InviteRequest
    with pytest.raises((ValidationError, ValueError)):
        InviteRequest(email="x@y.com", role="superuser")


def test_invite_request_bad_team_slug():
    from brain.wrappers.api import InviteRequest
    with pytest.raises((ValidationError, ValueError)):
        InviteRequest(email="x@y.com", role="employee", team_id="Bad Team ID!")


def test_member_patch_bad_role():
    from brain.wrappers.api import MemberPatchRequest
    with pytest.raises((ValidationError, ValueError)):
        MemberPatchRequest(role="overlord")


def test_member_patch_bad_status():
    from brain.wrappers.api import MemberPatchRequest
    with pytest.raises((ValidationError, ValueError)):
        MemberPatchRequest(status="suspended")


def test_message_request_bad_role():
    from brain.wrappers.api import MessageRequest
    with pytest.raises((ValidationError, ValueError)):
        MessageRequest(role="system", content="hello")


def test_message_request_empty_content():
    from brain.wrappers.api import MessageRequest
    with pytest.raises((ValidationError, ValueError)):
        MessageRequest(role="user", content="   ")


def test_message_request_content_too_long():
    from brain.wrappers.api import MessageRequest
    with pytest.raises((ValidationError, ValueError)):
        MessageRequest(role="user", content="x" * 100_001)


def test_conversation_request_title_too_long():
    from brain.wrappers.api import ConversationRequest
    with pytest.raises((ValidationError, ValueError)):
        ConversationRequest(title="x" * 201)


def test_composio_connect_bad_slug():
    from brain.wrappers.api import ComposioConnectRequest
    with pytest.raises((ValidationError, ValueError)):
        ComposioConnectRequest(app="Gmail App!")


# ── Rate limiter ──────────────────────────────────────────────────────────────

def test_rate_limiter_allows_under_limit():
    from brain.wrappers.api import _rate_limit
    import unittest.mock as mock
    req = mock.MagicMock()
    req.client.host = "1.2.3.4"
    # Wipe any existing state for this IP
    from brain.wrappers.api import _rl_store
    _rl_store.pop("1.2.3.4", None)
    # Should not raise for 5 calls with limit=10
    for _ in range(5):
        _rate_limit(req, limit=10, window=60)


def test_rate_limiter_blocks_over_limit():
    from fastapi import HTTPException
    from brain.wrappers.api import _rate_limit, _rl_store
    import unittest.mock as mock
    req = mock.MagicMock()
    req.client.host = "9.9.9.9"
    _rl_store.pop("9.9.9.9", None)
    # Fill up to limit
    for _ in range(3):
        _rate_limit(req, limit=3, window=60)
    # 4th call should raise
    with pytest.raises(HTTPException) as exc:
        _rate_limit(req, limit=3, window=60)
    assert exc.value.status_code == 429


def test_rate_limiter_evicts_old_entries():
    """Entries outside the window should be evicted, allowing new requests."""
    import time
    from brain.wrappers.api import _rate_limit, _rl_store
    import unittest.mock as mock
    req = mock.MagicMock()
    req.client.host = "5.5.5.5"
    # Insert old timestamps (2 minutes ago — outside a 60s window)
    old_time = time.monotonic() - 120
    _rl_store["5.5.5.5"] = [old_time, old_time, old_time]
    # Should not raise even though 3 old entries exist
    _rate_limit(req, limit=3, window=60)  # evicts old, adds new → 1 entry
