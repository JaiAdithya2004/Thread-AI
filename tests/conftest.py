"""
Shared pytest fixtures for Thread test suite.

Tests that require a live Postgres connection are skipped automatically when
DATABASE_URL is not set. LLM and embed calls are always stubbed.
"""
import os
import json
import uuid
import unittest.mock as mock
import pytest
from dotenv import load_dotenv

load_dotenv()

POSTGRES_AVAILABLE = bool(os.getenv("DATABASE_URL"))

# ── Environment flags ─────────────────────────────────────────────────────────
os.environ["BRAIN_ENV"]        = "development"
os.environ["BRAIN_DEV_BYPASS"] = "true"

_STUB_FACTS = {"facts": [{"subject": "pytest", "predicate": "is", "object": "great",
                           "summary": "pytest is great", "confidence": 0.9}], "terms": []}
_STUB_ANSWER = "pytest is great"


@pytest.fixture(autouse=True)
def _restore_auth_bypass():
    """After each test, reload brain.auth so _BYPASS is re-evaluated from current env.
    Guards against auth tests that call importlib.reload(brain.auth) leaving _BYPASS=False."""
    yield
    import importlib
    import brain.auth
    importlib.reload(brain.auth)


@pytest.fixture(autouse=True)
def stub_llm(monkeypatch):
    """Patch litellm.completion so every test gets a deterministic LLM response."""
    fake_usage = mock.MagicMock()
    fake_usage.prompt_tokens = 10
    fake_usage.completion_tokens = 5

    def _fake_completion(**kwargs):
        resp = mock.MagicMock()
        resp.usage = fake_usage
        content = json.dumps(_STUB_FACTS) if kwargs.get("response_format") else _STUB_ANSWER
        resp.choices = [mock.MagicMock()]
        resp.choices[0].message.content = content
        return resp

    monkeypatch.setattr("litellm.completion", _fake_completion, raising=False)
    # Also patch the internal wrapper used by llm.py
    import brain.core.llm as _llm
    monkeypatch.setattr(_llm, "_completion", _fake_completion)
    yield


@pytest.fixture(autouse=True)
def stub_embed(monkeypatch):
    """Patch all embed functions to return fixed 768-dim vectors — prevents Gemini API calls."""
    import brain.core.embed as _embed
    from brain.config import EMBED_DIM

    # Stub the internal Gemini call so no network is needed
    monkeypatch.setattr(_embed, "_call_gemini",
                        lambda batch, task_type: [[0.0] * EMBED_DIM for _ in batch])

    # Stub the public API functions directly
    monkeypatch.setattr(_embed, "embed",       lambda text: [0.0] * EMBED_DIM)
    monkeypatch.setattr(_embed, "embed_batch", lambda texts: [[0.0] * EMBED_DIM for _ in texts])

    yield


@pytest.fixture
def ws_id():
    """A unique workspace ID per test so state never leaks between tests."""
    return f"ws_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def pg_conn(ws_id):
    """Postgres connection for integration tests. Skipped when DATABASE_URL is not set."""
    if not POSTGRES_AVAILABLE:
        pytest.skip("requires Postgres DATABASE_URL")
    from brain.core import db
    conn = db.connect()
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def brain(ws_id, pg_conn):
    """A Brain instance backed by a Postgres connection. Skipped when DATABASE_URL is not set."""
    from brain.engine import Brain
    b = Brain.__new__(Brain)
    b.ws         = ws_id
    b.team_id    = "test-team"
    b.caller     = "tester@example.com"
    b.principals = ["tester@example.com", "everyone"]
    b.account_id = None
    b.conn       = pg_conn
    from brain.core import db
    db.migrate(b.conn)
    return b


@pytest.fixture
def api_client():
    """FastAPI TestClient with auth bypass (dev mode)."""
    # Must import AFTER env vars are set
    from fastapi.testclient import TestClient
    from brain.wrappers.api import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers():
    """Auth headers for bypass mode — empty bearer triggers bypass identity."""
    return {"Authorization": "Bearer test-token-dev"}
