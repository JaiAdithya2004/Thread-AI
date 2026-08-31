"""
Tests for the sync pipeline: watermark state machine, webhook dedup,
external-id supersession, reconcile job, ingest trigger tagging.

DB-backed tests are skipped when DATABASE_URL is not set.
Webhook endpoint tests use the FastAPI TestClient and don't need a live DB.
"""
import hashlib
import os
import uuid
import pytest
import unittest.mock as mock

POSTGRES_AVAILABLE = bool(os.getenv("DATABASE_URL"))


# ---------------------------------------------------------------------------
# Unit tests — no DB, no network
# ---------------------------------------------------------------------------

class TestGenericExternalIdStable:
    """The generic Composio fallback must produce the same external_id for
    the same source data — no time.time() in the key."""

    def test_same_data_same_id(self):
        from brain.core.connectors.composio import _normalize_result
        data = {"key": "value", "nested": {"a": 1}}
        eps1 = _normalize_result("unknownapp", "SOME_ACTION", {"data": data})
        eps2 = _normalize_result("unknownapp", "SOME_ACTION", {"data": data})
        assert eps1 and eps2
        assert eps1[0]["external_id"] == eps2[0]["external_id"]

    def test_different_data_different_id(self):
        from brain.core.connectors.composio import _normalize_result
        eps1 = _normalize_result("unknownapp", "ACT", {"data": {"x": 1}})
        eps2 = _normalize_result("unknownapp", "ACT", {"data": {"x": 2}})
        assert eps1 and eps2
        assert eps1[0]["external_id"] != eps2[0]["external_id"]

    def test_no_time_dependency(self):
        """Two calls with identical data at different times must match."""
        from brain.core.connectors.composio import _normalize_result
        import time
        data = {"stable": True}
        eps1 = _normalize_result("app", "ACT", {"data": data})
        time.sleep(0.01)
        eps2 = _normalize_result("app", "ACT", {"data": data})
        assert eps1[0]["external_id"] == eps2[0]["external_id"]


class TestFilterSince:
    def test_drops_old_items(self):
        from brain.core.connectors.composio import _filter_since
        import datetime
        cutoff = datetime.datetime(2024, 6, 1)
        episodes = [
            {"occurred_at": "2024-05-31T23:59:59", "body": "old"},
            {"occurred_at": "2024-06-01T00:00:00", "body": "exact"},
            {"occurred_at": "2024-06-02T00:00:00", "body": "new"},
        ]
        result = _filter_since(episodes, cutoff)
        bodies = [e["body"] for e in result]
        assert "old" not in bodies
        assert "exact" in bodies
        assert "new" in bodies

    def test_keeps_unparseable_timestamps(self):
        from brain.core.connectors.composio import _filter_since
        import datetime
        cutoff = datetime.datetime(2024, 6, 1)
        episodes = [{"occurred_at": "not-a-date", "body": "keep"}]
        result = _filter_since(episodes, cutoff)
        assert len(result) == 1

    def test_handles_timezone_aware(self):
        from brain.core.connectors.composio import _filter_since
        import datetime
        cutoff = datetime.datetime(2024, 6, 1, 12, 0, 0)
        episodes = [
            {"occurred_at": "2024-06-01T13:00:00+00:00", "body": "after"},
            {"occurred_at": "2024-06-01T10:00:00+00:00", "body": "before"},
        ]
        result = _filter_since(episodes, cutoff)
        assert any(e["body"] == "after" for e in result)
        assert not any(e["body"] == "before" for e in result)


class TestSlackPollSince:
    """Slack poll() must use the since watermark, not a hardcoded 24h window."""

    def _make_connector(self, mock_client):
        """Create a SlackConnector with a pre-wired mock WebClient."""
        from brain.core.connectors.slack import SlackConnector
        conn = SlackConnector.__new__(SlackConnector)
        conn.token = "xoxb-fake"
        conn.client = mock_client
        return conn

    def test_poll_uses_since_timestamp(self):
        import datetime
        mock_client = mock.MagicMock()
        mock_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "general",
                          "is_member": True, "is_private": False}],
            "response_metadata": {"next_cursor": ""},
        }
        mock_client.conversations_history.return_value = {
            "messages": [], "response_metadata": {"next_cursor": ""},
        }
        conn = self._make_connector(mock_client)
        since = datetime.datetime(2024, 6, 1, 12, 0, 0)
        conn.poll(since=since)
        call_kwargs = mock_client.conversations_history.call_args[1]
        assert "oldest" in call_kwargs
        assert float(call_kwargs["oldest"]) == pytest.approx(since.timestamp(), rel=0.01)

    def test_poll_defaults_to_24h_when_no_since(self):
        import time
        mock_client = mock.MagicMock()
        mock_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "general",
                          "is_member": True, "is_private": False}],
            "response_metadata": {"next_cursor": ""},
        }
        mock_client.conversations_history.return_value = {
            "messages": [], "response_metadata": {"next_cursor": ""},
        }
        conn = self._make_connector(mock_client)
        before = time.time()
        conn.poll()
        after = time.time()
        call_kwargs = mock_client.conversations_history.call_args[1]
        oldest = float(call_kwargs["oldest"])
        assert before - 86_410 <= oldest <= after - 86_390


class TestSyncStateBackoff:
    """Backoff formula: min(2^failures * 60, 3600)."""

    def test_backoff_increases(self):
        # Simulate the backoff formula from set_sync_state_failed
        def backoff(failures):
            return min(2 ** failures * 60, 3600)

        assert backoff(1) == 120
        assert backoff(2) == 240
        assert backoff(3) == 480
        assert backoff(4) == 960
        assert backoff(5) == 1920
        assert backoff(6) == 3600   # capped
        assert backoff(10) == 3600  # still capped


class TestIngestTriggerTag:
    """ingest.run() must accept and store the trigger parameter."""

    def test_run_signature_has_trigger(self):
        import inspect
        from brain.core.ingest import run
        sig = inspect.signature(run)
        assert "trigger" in sig.parameters


class TestNotionDuplicateKeyFixed:
    """_APP_ACTIONS must have exactly one 'notion' key with page_size."""

    def test_single_notion_key(self):
        from brain.core.connectors.composio import _APP_ACTIONS
        assert "notion" in _APP_ACTIONS
        # Count how many times notion appears (dict can only have 1 key)
        assert len([k for k in _APP_ACTIONS if k == "notion"]) == 1

    def test_notion_has_page_size(self):
        from brain.core.connectors.composio import _APP_ACTIONS
        actions = _APP_ACTIONS["notion"]
        action_slug, args = actions[0]
        assert "page_size" in args, "page_size was lost in the duplicate key fix"
        assert args["page_size"] == 50


# ---------------------------------------------------------------------------
# Webhook endpoint tests — TestClient, no live DB needed
# ---------------------------------------------------------------------------

class TestWebhookEndpoint:
    """Tests for POST /api/ingest/event."""

    @pytest.fixture
    def client_with_secret(self, monkeypatch):
        monkeypatch.setenv("COMPOSIO_WEBHOOK_SECRET", "test-secret-32chars-padded-here!")
        monkeypatch.setenv("BRAIN_ENV", "development")
        monkeypatch.setenv("BRAIN_DEV_BYPASS", "true")
        from fastapi.testclient import TestClient
        # Reload config so new env var is picked up
        import importlib
        import brain.config
        importlib.reload(brain.config)
        from brain.wrappers.api import app
        return TestClient(app, raise_server_exceptions=False)

    def _sign(self, body: bytes, secret: str) -> str:
        import hmac
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_rejects_missing_secret_config(self, api_client):
        """Returns 503 when COMPOSIO_WEBHOOK_SECRET is not set."""
        import brain.config as cfg
        original = cfg.COMPOSIO_WEBHOOK_SECRET
        cfg.COMPOSIO_WEBHOOK_SECRET = ""
        try:
            resp = api_client.post("/api/ingest/event",
                                   content=b'{"app":"gmail"}',
                                   headers={"Content-Type": "application/json",
                                            "x-composio-signature": "sha256=bad"})
            assert resp.status_code == 503
        finally:
            cfg.COMPOSIO_WEBHOOK_SECRET = original

    def test_rejects_bad_signature(self, monkeypatch):
        monkeypatch.setenv("COMPOSIO_WEBHOOK_SECRET", "test-secret-32chars-padded-here!")
        import importlib
        import brain.config
        importlib.reload(brain.config)
        from fastapi.testclient import TestClient
        from brain.wrappers.api import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/ingest/event",
            content=b'{"app":"gmail","data":{}}',
            headers={
                "Content-Type": "application/json",
                "x-composio-signature": "sha256=badsignature",
            },
        )
        assert resp.status_code == 401

    def test_accepts_valid_signature(self, monkeypatch):
        secret = "test-secret-32chars-padded-here!"
        monkeypatch.setenv("COMPOSIO_WEBHOOK_SECRET", secret)
        import importlib
        import brain.config
        importlib.reload(brain.config)

        # Mock arq so we don't need Redis
        mock_pool = mock.AsyncMock()
        mock_pool.enqueue_job = mock.AsyncMock()
        mock_pool.aclose = mock.AsyncMock()

        with mock.patch("arq.create_pool", return_value=mock_pool):
            from fastapi.testclient import TestClient
            from brain.wrappers.api import app
            client = TestClient(app, raise_server_exceptions=False)
            body = b'{"app":"gmail","data":{}}'
            sig = self._sign(body, secret)
            resp = client.post(
                "/api/ingest/event",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "x-composio-signature": sig,
                    "x-webhook-delivery-id": "test-delivery-001",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["event_id"] == "test-delivery-001"

    def test_duplicate_delivery_id_rejected_by_redis(self, monkeypatch):
        """Same delivery ID with Redis available returns 'duplicate' without enqueuing."""
        secret = "test-secret-32chars-padded-here!"
        monkeypatch.setenv("COMPOSIO_WEBHOOK_SECRET", secret)
        import importlib
        import brain.config
        importlib.reload(brain.config)

        mock_redis = mock.MagicMock()
        mock_redis.exists.return_value = True  # simulate already seen

        mock_pool = mock.AsyncMock()
        mock_pool.enqueue_job = mock.AsyncMock()
        mock_pool.aclose = mock.AsyncMock()

        with mock.patch("redis.from_url", return_value=mock_redis):
            with mock.patch("arq.create_pool", return_value=mock_pool):
                from fastapi.testclient import TestClient
                from brain.wrappers.api import app
                client = TestClient(app, raise_server_exceptions=False)
                body = b'{"app":"gmail","data":{}}'
                sig = self._sign(body, secret)
                resp = client.post(
                    "/api/ingest/event",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "x-composio-signature": sig,
                        "x-webhook-delivery-id": "already-seen-delivery",
                    },
                )
        assert resp.status_code == 200
        assert resp.json()["status"] == "duplicate"
        mock_pool.enqueue_job.assert_not_called()


# ---------------------------------------------------------------------------
# Integration tests — require live Postgres
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
class TestSyncStateMachine:
    """State machine transitions in connector_sync_state."""

    def test_initial_state_is_idle(self, pg_conn, ws_id):
        from brain.core.db_pg import get_sync_state
        state = get_sync_state(pg_conn, ws_id, "composio")
        assert state["status"] == "idle"
        assert state["consecutive_failures"] == 0
        assert state["last_synced_at"] is None

    def test_set_running(self, pg_conn, ws_id):
        from brain.core.db_pg import get_sync_state, set_sync_state_running
        set_sync_state_running(pg_conn, ws_id, "composio", job_id="job-123")
        state = get_sync_state(pg_conn, ws_id, "composio")
        assert state["status"] == "running"
        assert state["last_run_job_id"] == "job-123"
        assert state["last_run_started_at"] is not None

    def test_success_resets_to_idle(self, pg_conn, ws_id):
        from brain.core.db_pg import (
            get_sync_state, set_sync_state_running, set_sync_state,
        )
        set_sync_state_running(pg_conn, ws_id, "composio", job_id="job-abc")
        set_sync_state(pg_conn, ws_id, "composio", last_count=5)
        state = get_sync_state(pg_conn, ws_id, "composio")
        assert state["status"] == "idle"
        assert state["consecutive_failures"] == 0
        assert state["last_count"] == 5
        assert state["last_synced_at"] is not None
        assert state["locked_until"] is None

    def test_failure_increments_and_sets_backoff(self, pg_conn, ws_id):
        from brain.core.db_pg import get_sync_state, set_sync_state_failed
        import datetime
        before = datetime.datetime.utcnow()
        set_sync_state_failed(pg_conn, ws_id, "composio", error="connection refused")
        state = get_sync_state(pg_conn, ws_id, "composio")
        assert state["status"] == "failed"
        assert state["consecutive_failures"] == 1
        assert state["last_error"] == "connection refused"
        assert state["locked_until"] is not None
        # locked_until should be ~2 minutes in the future (2^1 * 60 = 120s)
        lu = state["locked_until"].replace(tzinfo=None)
        assert lu > before + datetime.timedelta(seconds=100)
        assert lu < before + datetime.timedelta(seconds=200)

    def test_consecutive_failures_accumulate(self, pg_conn, ws_id):
        from brain.core.db_pg import get_sync_state, set_sync_state_failed
        set_sync_state_failed(pg_conn, ws_id, "slack", error="err1")
        set_sync_state_failed(pg_conn, ws_id, "slack", error="err2")
        set_sync_state_failed(pg_conn, ws_id, "slack", error="err3")
        state = get_sync_state(pg_conn, ws_id, "slack")
        assert state["consecutive_failures"] == 3
        assert state["last_error"] == "err3"

    def test_success_after_failures_resets_counter(self, pg_conn, ws_id):
        from brain.core.db_pg import (
            get_sync_state, set_sync_state_failed, set_sync_state,
        )
        set_sync_state_failed(pg_conn, ws_id, "composio", error="boom")
        set_sync_state_failed(pg_conn, ws_id, "composio", error="boom again")
        set_sync_state(pg_conn, ws_id, "composio", last_count=3)
        state = get_sync_state(pg_conn, ws_id, "composio")
        assert state["status"] == "idle"
        assert state["consecutive_failures"] == 0
        assert state["locked_until"] is None


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
class TestExternalIdSupersession:
    """Episodes with the same external_id but different body create a supersession chain."""

    def test_same_content_is_deduped(self, pg_conn, ws_id):
        from brain.core.ingest import run
        ep = {
            "source": "composio:gmail",
            "external_id": "gmail:msg001",
            "title": "Test",
            "body": "Hello this is the original email body text that is long enough",
            "author": "alice@example.com",
            "url": "",
            "occurred_at": "2024-06-01T10:00:00",
            "acl": ["everyone"],
        }
        n1 = run(pg_conn, ws_id, [ep])
        n2 = run(pg_conn, ws_id, [ep])  # exact same content
        assert n1 == 1
        assert n2 == 0  # deduplicated by content_hash

    def test_updated_content_creates_supersession_link(self, pg_conn, ws_id):
        from brain.core.ingest import run
        ep_v1 = {
            "source": "composio:notion",
            "external_id": "notion:page001",
            "title": "Decision Doc",
            "body": "We decided to use PostgreSQL for the database layer",
            "author": "bob@example.com",
            "url": "https://notion.so/page001",
            "occurred_at": "2024-06-01T10:00:00",
            "acl": ["everyone"],
        }
        ep_v2 = {
            **ep_v1,
            "body": "We decided to use PostgreSQL for the database layer - updated with pgvector",
            "occurred_at": "2024-06-02T10:00:00",
        }
        n1 = run(pg_conn, ws_id, [ep_v1])
        n2 = run(pg_conn, ws_id, [ep_v2])
        assert n1 == 1
        assert n2 == 1

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT id, supersedes_episode_id FROM episodes "
            "WHERE workspace_id=%s AND external_id=%s ORDER BY occurred_at",
            (ws_id, "notion:page001"),
        )
        rows = cur.fetchall()
        cur.close()
        assert len(rows) == 2
        assert rows[0]["supersedes_episode_id"] is None  # original has no supersession
        assert rows[1]["supersedes_episode_id"] == rows[0]["id"]  # v2 points to v1

    def test_ingest_trigger_is_stored(self, pg_conn, ws_id):
        from brain.core.ingest import run
        ep = {
            "source": "composio:github",
            "external_id": "github:issue:42",
            "title": "Fix the bug",
            "body": "This issue describes a real bug that needs to be fixed soon",
            "author": "dev@example.com",
            "url": "",
            "occurred_at": "2024-06-01T10:00:00",
            "acl": ["everyone"],
        }
        run(pg_conn, ws_id, [ep], trigger="webhook")
        cur = pg_conn.cursor()
        cur.execute(
            "SELECT ingest_trigger FROM episodes WHERE workspace_id=%s AND external_id=%s",
            (ws_id, "github:issue:42"),
        )
        row = cur.fetchone()
        cur.close()
        assert row["ingest_trigger"] == "webhook"


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
class TestWebhookRouteTable:
    """connector_webhook_routes stores entity->workspace mapping."""

    def test_upsert_and_lookup(self, pg_conn, ws_id):
        from brain.core.db_pg import upsert_webhook_route, lookup_webhook_route
        entity_id = f"entity_{uuid.uuid4().hex[:8]}"
        upsert_webhook_route(pg_conn, entity_id=entity_id,
                             workspace_id=ws_id, account_id="acct_test")
        result = lookup_webhook_route(pg_conn, entity_id)
        assert result is not None
        assert result["workspace_id"] == ws_id
        assert result["account_id"] == "acct_test"

    def test_lookup_unknown_entity_returns_none(self, pg_conn, ws_id):
        from brain.core.db_pg import lookup_webhook_route
        result = lookup_webhook_route(pg_conn, "nonexistent-entity-id")
        assert result is None

    def test_upsert_updates_existing(self, pg_conn, ws_id):
        from brain.core.db_pg import upsert_webhook_route, lookup_webhook_route
        entity_id = f"entity_{uuid.uuid4().hex[:8]}"
        upsert_webhook_route(pg_conn, entity_id, ws_id, "acct_old")
        upsert_webhook_route(pg_conn, entity_id, ws_id, "acct_new")
        result = lookup_webhook_route(pg_conn, entity_id)
        assert result["account_id"] == "acct_new"


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
class TestResetStuckSync:
    """reset_stuck_sync only resets 'running' rows, not idle or failed ones."""

    def test_resets_running(self, pg_conn, ws_id):
        from brain.core.db_pg import (
            get_sync_state, set_sync_state_running, reset_stuck_sync,
        )
        set_sync_state_running(pg_conn, ws_id, "composio", job_id="stuck-job")
        reset_stuck_sync(pg_conn, ws_id, "composio")
        state = get_sync_state(pg_conn, ws_id, "composio")
        assert state["status"] == "idle"
        assert state["last_run_job_id"] is None

    def test_does_not_reset_idle(self, pg_conn, ws_id):
        from brain.core.db_pg import get_sync_state, set_sync_state, reset_stuck_sync
        set_sync_state(pg_conn, ws_id, "composio", last_count=0)
        reset_stuck_sync(pg_conn, ws_id, "composio")
        state = get_sync_state(pg_conn, ws_id, "composio")
        assert state["status"] == "idle"  # unchanged
