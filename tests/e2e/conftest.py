"""
E2E test fixtures — seeds the DB, starts the FastAPI server in-process, and
provides Playwright browser instances. Runs headless; captures screenshots +
traces on failure.

Requirements:
    pip install playwright pytest-playwright
    playwright install chromium

Run:
    pytest tests/e2e/ -v --headed=false
"""
import os
import uuid
import json
import threading
import time
import unittest.mock as mock
import pytest
import psycopg2
import psycopg2.extras

# ── Server env must be set before any brain imports ───────────────────────────
os.environ.setdefault("BRAIN_ENV",        "development")
os.environ.setdefault("BRAIN_DEV_BYPASS", "true")
os.environ.setdefault("BRAIN_EMBED_OPENAI", "false")

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    pytest.skip("E2E tests require DATABASE_URL", allow_module_level=True)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
BASE_URL   = "http://127.0.0.1:7171"   # dedicated E2E port avoids collisions

_STUB_FACTS  = {"facts": [{"subject": "thread", "predicate": "is",
                            "object": "company memory",
                            "summary": "Thread is company memory",
                            "confidence": 0.9}], "terms": []}
_STUB_ANSWER = "Thread is your AI-native company memory."


@pytest.fixture(scope="session", autouse=True)
def stub_llm_session():
    """Session-scoped LLM stub so no real API call is made during E2E."""
    fake_usage = mock.MagicMock()
    fake_usage.prompt_tokens = 10
    fake_usage.completion_tokens = 5

    def _fake(**kwargs):
        r = mock.MagicMock()
        r.usage = fake_usage
        content = json.dumps(_STUB_FACTS) if kwargs.get("response_format") else _STUB_ANSWER
        r.choices = [mock.MagicMock()]
        r.choices[0].message.content = content
        return r

    import brain.core.llm as _llm
    patcher = mock.patch.object(_llm, "_completion", _fake)
    patcher.start()
    yield
    patcher.stop()


@pytest.fixture(scope="session", autouse=True)
def stub_embed_session():
    """Session-scoped embed stub — uses 768-dim to match gemini-embedding-001."""
    import brain.core.embed as _embed
    from brain.config import EMBED_DIM
    p1 = mock.patch.object(_embed, "embed",       lambda t: [0.0] * EMBED_DIM)
    p2 = mock.patch.object(_embed, "embed_batch", lambda ts: [[0.0] * EMBED_DIM for _ in ts])
    p1.start()
    p2.start()
    yield
    p1.stop()
    p2.stop()


# ── FastAPI server thread ─────────────────────────────────────────────────────
def _run_server():
    import uvicorn
    from brain.wrappers.api import app
    uvicorn.run(app, host="127.0.0.1", port=7171, log_level="error")


@pytest.fixture(scope="session")
def live_server():
    """Start the FastAPI app in a daemon thread and wait until it's ready."""
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    import httpx
    for _ in range(40):
        try:
            httpx.get(f"{BASE_URL}/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        pytest.fail("E2E server did not start within 20 seconds")
    yield BASE_URL


# ── DB helpers ────────────────────────────────────────────────────────────────
def _pg():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def _migrate():
    from brain.core import db
    conn = db.connect()
    db.migrate(conn)
    conn.close()


@pytest.fixture(scope="session", autouse=True)
def db_ready():
    """Run migrations once before any E2E test."""
    _migrate()


@pytest.fixture
def db_conn():
    """Per-test DB connection; rolls back after each test for isolation."""
    conn = _pg()
    yield conn
    conn.rollback()
    conn.close()


def seed_account(conn, user_id: str, account_type: str = "individual",
                 name: str = "Test Co") -> dict:
    """Create account + owner membership + free subscription. Returns dict."""
    cur = conn.cursor()
    account_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO accounts (id, type, name) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
        (account_id, account_type, name),
    )
    cur.execute(
        "INSERT INTO memberships (id, user_id, account_id, role, status) "
        "VALUES (%s,%s,%s,'owner','active') ON CONFLICT DO NOTHING",
        (str(uuid.uuid4()), user_id, account_id),
    )
    conn.commit()
    cur.close()
    return {"account_id": account_id, "user_id": user_id}


def seed_fact(conn, ws: str, summary: str, team_id: str = "shared") -> str:
    """Insert a fact directly into the DB and return its id."""
    import uuid as _u
    cur = conn.cursor()
    fact_id = str(_u.uuid4())
    embedding = [0.0] * 384
    cur.execute("""
        INSERT INTO facts (id, workspace_id, subject, predicate, object, summary,
                           confidence, team_id, embedding, source_kind)
        VALUES (%s, %s, 'test', 'is', 'a test', %s, 0.9, %s,
                %s::vector, 'human')
        ON CONFLICT DO NOTHING
    """, (fact_id, ws, summary, team_id, str(embedding)))
    cur.execute("""
        INSERT INTO teams (id, workspace_id, name) VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """, (team_id, ws, team_id))
    conn.commit()
    cur.close()
    return fact_id


# ── Playwright fixtures ───────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def browser_context_args():
    """Base browser context — ignore HTTPS errors since we're on localhost."""
    return {"ignore_https_errors": True, "base_url": BASE_URL}


@pytest.fixture
def page(browser, live_server, tmp_path, request):
    """
    Per-test Playwright page.
    On failure: capture screenshot + trace to tmp_path and attach to report.
    """
    context = browser.new_context(base_url=BASE_URL, record_video_dir=None)
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    pg = context.new_page()

    yield pg

    # Failure handling
    failed = request.node.rep_call.failed if hasattr(request.node, "rep_call") else False
    if failed:
        shot_path = str(tmp_path / f"{request.node.name}.png")
        trace_path = str(tmp_path / f"{request.node.name}.zip")
        pg.screenshot(path=shot_path)
        context.tracing.stop(path=trace_path)
        print(f"\n[e2e] Screenshot: {shot_path}")
        print(f"[e2e] Trace:      {trace_path}")
    else:
        context.tracing.stop()

    pg.close()
    context.close()


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Store test outcome on node so the `page` fixture can check it."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)
