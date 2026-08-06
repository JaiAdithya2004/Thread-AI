"""
Shared pytest fixtures for Intellark test suite.

All fixtures use SQLite (in-memory) so tests run without Postgres.
LLM calls are stubbed out so tests never hit a real API.
"""
import os, json, uuid, pytest

# ── Force SQLite backend for all tests ───────────────────────────────────────
# Use hard assignment (not setdefault) so these win over .env loaded by config.py
os.environ["BRAIN_DB_BACKEND"]  = "sqlite"
os.environ["BRAIN_ENV"]         = "development"
os.environ["BRAIN_DEV_BYPASS"]  = "true"
os.environ["BRAIN_EMBED_OPENAI"] = "false"

# ── Stub LLM so no real API keys are needed ───────────────────────────────────
import unittest.mock as mock

_STUB_FACTS = {"facts": [{"subject": "pytest", "predicate": "is", "object": "great",
                           "summary": "pytest is great", "confidence": 0.9}], "terms": []}
_STUB_ANSWER = "pytest is great"


@pytest.fixture(autouse=True)
def _restore_auth_bypass():
    """After each test, reload brain.auth so _BYPASS is re-evaluated from current env.
    Guards against auth tests that call importlib.reload(brain.auth) leaving _BYPASS=False."""
    yield
    import importlib, brain.auth
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
    """Patch all embed functions to return fixed 384-dim vectors — prevents fastembed download."""
    import brain.core.embed as _embed

    # Stub the cached model so it's never invoked
    fake_model = mock.MagicMock()
    fake_model.embed = lambda texts: ([0.0] * 384 for _ in texts)
    monkeypatch.setattr(_embed, "_m", lambda: fake_model)

    # Stub the public API functions directly
    monkeypatch.setattr(_embed, "embed",       lambda text: [0.0] * 384)
    monkeypatch.setattr(_embed, "embed_batch", lambda texts: [[0.0] * 384 for _ in texts])

    yield


@pytest.fixture
def ws_id():
    """A unique workspace ID per test so state never leaks between tests."""
    return f"ws_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def sqlite_conn(ws_id):
    """Fresh in-memory SQLite connection with schema migrated."""
    from brain.core import db
    conn = db.connect()
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def brain(ws_id, sqlite_conn):
    """A Brain instance backed by the in-memory SQLite connection."""
    from brain.engine import Brain
    b = Brain.__new__(Brain)
    b.ws         = ws_id
    b.team_id    = "test-team"
    b.caller     = "tester@example.com"
    b.principals = ["tester@example.com", "everyone"]
    b.account_id = None
    b.conn       = sqlite_conn
    from brain.core import db
    db.migrate(b.conn)
    return b


@pytest.fixture
def api_client():
    """FastAPI TestClient with auth bypass (dev mode)."""
    # Must import AFTER env vars are set
    from fastapi.testclient import TestClient
    from brain.wrappers.api import app
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def auth_headers():
    """Auth headers for bypass mode — empty bearer triggers bypass identity."""
    return {"Authorization": "Bearer test-token-dev"}
