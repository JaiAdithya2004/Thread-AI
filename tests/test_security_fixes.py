"""
Regression tests for the defects found while documenting the architecture:

- `BACKEND` must stay importable from brain.core.db (plan limits, usage reporting
  and backfill all import it).
- Roles, teams and workspaces are read only from Clerk's trusted metadata.
- The integration connect flow seals the caller's identity into a signed state,
  and the callback trusts nothing else.
- Signup takes the user id from the verified session, not from the request body.
"""
import time
import unittest.mock as mock
from urllib.parse import parse_qs, urlparse

import pytest


# ── BACKEND is importable and the code that needs it runs ────────────────────

def test_backend_is_exported():
    from brain.core.db import BACKEND
    assert BACKEND == "postgres"


def _bare_brain(account_id=None):
    from brain.engine import Brain
    b = Brain.__new__(Brain)
    b.ws, b.team_id, b.caller = "ws", "team", "me"
    b.principals, b.account_id, b.conn = ["me"], account_id, None
    return b


def test_plan_without_account_is_the_free_plan():
    plan = _bare_brain()._plan()
    assert plan["name"] == "free" and plan["max_seats"] == 1


def test_backfill_limit_check_no_longer_raises_import_error():
    # Before the fix this raised ImportError, which Brain.backfill does not catch.
    _bare_brain()._check_backfill_limit(5)


def test_usage_summary_without_account_is_empty_not_an_error():
    out = _bare_brain().usage_summary()
    assert out["tokens"] == 0 and out["by_task"] == []


# ── only trusted metadata grants a role ──────────────────────────────────────

def test_unsafe_metadata_cannot_grant_admin():
    from brain.auth import _identity_from_clerk_payload
    ident = _identity_from_clerk_payload({
        "sub": "user_1",
        "unsafe_metadata": {"role": "admin", "team_id": "someone-elses", "workspace_id": "other"},
    })
    assert ident.role == "member" and ident.is_admin is False
    assert ident.team_id != "someone-elses" and ident.workspace_id != "other"


def test_public_metadata_still_grants_a_role():
    from brain.auth import _identity_from_clerk_payload
    ident = _identity_from_clerk_payload(
        {"sub": "u", "public_metadata": {"role": "admin", "team_id": "eng", "workspace_id": "ws1"}})
    assert ident.is_admin and ident.team_id == "eng" and ident.workspace_id == "ws1"


# ── signed OAuth state ───────────────────────────────────────────────────────

def test_state_round_trip():
    from brain.auth import sign_state, verify_state
    token = sign_state({"user_id": "u1", "workspace_id": "w1"})
    got = verify_state(token)
    assert got["user_id"] == "u1" and got["workspace_id"] == "w1"


def test_tampered_state_is_rejected():
    from brain.auth import sign_state, verify_state
    token = sign_state({"user_id": "u1", "workspace_id": "w1"})
    body, sig = token.rsplit(".", 1)
    with pytest.raises(ValueError):
        verify_state(body + "." + ("0" * len(sig)))
    with pytest.raises(ValueError):
        verify_state(("A" + body[1:]) + "." + sig)


@pytest.mark.parametrize("junk", ["", "nodot", "a.b", "....", None])
def test_malformed_state_is_rejected(junk):
    from brain.auth import verify_state
    with pytest.raises(ValueError):
        verify_state(junk)


def test_expired_state_is_rejected():
    from brain.auth import sign_state, verify_state
    token = sign_state({"user_id": "u1"}, ttl_seconds=-1)
    with pytest.raises(ValueError):
        verify_state(token)


# ── connect + callback ───────────────────────────────────────────────────────

def test_connect_builds_a_signed_callback_and_does_not_crash(api_client, auth_headers):
    captured = {}

    def fake_url(app, user_id=None, redirect_url=None):
        captured["redirect"] = redirect_url
        return "https://provider.example/connect"

    with mock.patch("brain.core.connectors.composio.get_connect_url", side_effect=fake_url), \
         mock.patch("brain.wrappers.api._resolve_account_id", return_value="acct-1"):
        r = api_client.post("/api/composio/connect", json={"app": "gmail"}, headers=auth_headers)

    assert r.status_code == 200, r.text   # used to be 500: Identity has no account_id
    q = parse_qs(urlparse(captured["redirect"]).query)
    assert set(q) == {"state"}            # no raw user_id / workspace_id / account_id
    from brain.auth import verify_state
    sealed = verify_state(q["state"][0])
    assert sealed["account_id"] == "acct-1" and sealed["user_id"]


def test_callback_refuses_a_missing_or_forged_state(api_client):
    with mock.patch("brain.core.db_pg.upsert_webhook_route") as up:
        assert api_client.get("/api/composio/callback", follow_redirects=False).status_code == 400
        r = api_client.get(
            "/api/composio/callback?user_id=victim&workspace_id=ws_demo&account_id=a",
            follow_redirects=False)
        assert r.status_code == 400
        r = api_client.get("/api/composio/callback?state=forged.sig", follow_redirects=False)
        assert r.status_code == 400
    up.assert_not_called()


def test_callback_uses_only_the_sealed_identity(api_client):
    from brain.auth import sign_state
    state = sign_state({"user_id": "real_user", "workspace_id": "real_ws", "account_id": "real_acct"})
    with mock.patch("brain.core.db_pg.connect", return_value=mock.MagicMock()), \
         mock.patch("brain.core.db_pg.upsert_webhook_route") as up:
        r = api_client.get(
            f"/api/composio/callback?state={state}&user_id=attacker&workspace_id=victim_ws",
            follow_redirects=False)
    assert r.status_code == 302
    kwargs = up.call_args.kwargs
    assert kwargs["entity_id"] == "real_user" and kwargs["workspace_id"] == "real_ws"


# ── signup ───────────────────────────────────────────────────────────────────

def test_signup_ignores_a_user_id_smuggled_in_the_body(api_client, auth_headers):
    from brain import config
    conn = mock.MagicMock()
    cur = conn.cursor.return_value
    cur.fetchone.return_value = None          # no existing membership
    with mock.patch("brain.core.db_pg.connect", return_value=conn), \
         mock.patch("brain.core.db_pg.ensure_free_subscription"):
        r = api_client.post("/api/auth/signup", headers=auth_headers, json={
            "email": "me@corp.example", "password": "victim_clerk_id",
            "account_type": "individual", "name": "Me"})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == config.CALLER           # the verified identity
    inserted = [c.args[1] for c in cur.execute.call_args_list
                if "INSERT INTO memberships" in c.args[0]]
    assert inserted and "victim_clerk_id" not in inserted[0]


def test_signup_requires_a_session_when_auth_is_enforced():
    """The route must depend on the identity check, so an anonymous caller gets 401."""
    from fastapi.routing import APIRoute
    from brain.wrappers.api import app
    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == "/api/auth/signup")
    # Compared by name: other test modules reload brain.auth, which changes the
    # function objects but not what the route depends on.
    names = {d.call.__name__ for d in route.dependant.dependencies}
    assert names & {"require_not_guest", "get_identity"}


# ── billing context is per request, not per process ─────────────────────────

def test_billing_context_is_isolated_between_threads():
    import threading
    from brain.core import llm
    seen = {}
    gate = threading.Barrier(4)

    def worker(n):
        llm._ctx["account_id"] = f"acct{n}"
        gate.wait()                       # all four have written before any reads
        seen[n] = llm._ctx.get("account_id")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert seen == {i: f"acct{i}" for i in range(4)}


def test_clear_empties_only_the_current_context():
    from brain.core import llm
    llm._ctx["account_id"] = "x"
    llm._ctx.clear()
    assert llm._ctx.get("account_id") is None


# ── a failed embedding must not be stored as a zero vector ──────────────────

def test_zero_vector_embeddings_are_refused():
    from brain import config
    from brain.core import extract
    ok = [[0.1] + [0.0] * (config.EMBED_DIM - 1)]
    assert extract._require_real_embeddings(ok) == ok
    with pytest.raises(extract.EmbeddingUnavailable):
        extract._require_real_embeddings([[0.0] * config.EMBED_DIM])
    with pytest.raises(extract.EmbeddingUnavailable):
        extract._require_real_embeddings(ok + [[0.0] * config.EMBED_DIM])


def test_harvest_saves_nothing_when_embeddings_are_down():
    from brain import config
    from brain.core import extract
    fact = {"facts": [{"subject": "s", "predicate": "p", "object": "o",
                       "summary": "A durable company fact.", "confidence": 0.9}]}
    with mock.patch.object(extract.llm, "chat_json", return_value=fact), \
         mock.patch.object(extract.embed, "embed_batch",
                           return_value=[[0.0] * config.EMBED_DIM]), \
         mock.patch.object(extract, "_save") as save:
        assert extract.extract_from_text(mock.MagicMock(), "ws", "We decided X.") == 0
    save.assert_not_called()


# ── never ask a provider to reserve a model's whole output limit ────────────

def test_llm_calls_always_carry_an_output_ceiling():
    import unittest.mock as mk
    from brain.core import llm
    captured = {}

    def fake_call(**kw):
        captured.update(kw)
        raise RuntimeError("stop here")        # we only care about the kwargs

    with mk.patch.object(llm, "_has_key", return_value=True), \
         mk.patch.object(llm, "_check_credit_allowance"), \
         mk.patch.object(llm, "_call_llm_with_fallback",
                         side_effect=lambda p, f, t, **kw: fake_call(**kw)):
        llm.chat_json("sys", "user", model="gemini/x", task_name="reconcile")
        assert captured["max_tokens"] == llm._DEFAULT_MAX_TOKENS["reconcile"]
        captured.clear()
        llm.chat_text("sys", "user", model="gemini/x", task_name="never-heard-of-it")
        assert captured["max_tokens"] == llm._FALLBACK_MAX_TOKENS
