"""
A person sees and can disconnect only their own connected apps.

Regression: the list endpoint returned every connection in the whole integration
project to any admin, and disconnect accepted any account id from any admin. With
a workspace per account, that would show one customer's connected accounts to
another's. No network: the integration provider is stubbed.
"""
import pytest
from fastapi import HTTPException

from brain.auth import Identity


def _ident(user_id, admin=True):
    return Identity(user_id=user_id, email=f"{user_id}@x.com", workspace_id=f"ws_{user_id}",
                    team_id="t", principals=[user_id, "everyone"],
                    role="owner" if admin else "member", is_admin=admin, is_guest=False)


@pytest.fixture
def provider(monkeypatch):
    """Two users' connections, held by one integration project."""
    import brain.core.connectors.composio as comp
    store = {
        "alice": [{"id": "acc_alice_gmail", "app": "gmail", "name": "Gmail", "status": "ACTIVE"}],
        "bob":   [{"id": "acc_bob_slack", "app": "slack", "name": "Slack", "status": "ACTIVE"}],
    }
    calls = {"list": [], "disconnected": []}

    def list_connected_apps(user_id=None):
        calls["list"].append(user_id)
        if user_id is None:                       # the old admin behaviour: everyone's
            return [a for v in store.values() for a in v]
        return list(store.get(user_id, []))

    monkeypatch.setattr(comp, "list_connected_apps", list_connected_apps)
    monkeypatch.setattr(comp, "disconnect_app", lambda aid: calls["disconnected"].append(aid) or {"ok": True})
    monkeypatch.setattr(comp, "deregister_webhook_trigger", lambda tid: None)
    return calls


def test_an_admin_lists_only_their_own_connections(provider):
    from brain.wrappers.api import composio_connected
    apps = composio_connected(identity=_ident("alice", admin=True))
    assert [a["id"] for a in apps] == ["acc_alice_gmail"]
    assert provider["list"] == ["alice"]            # never an unscoped, project-wide listing


def test_a_member_lists_only_their_own_connections(provider):
    from brain.wrappers.api import composio_connected
    assert [a["id"] for a in composio_connected(identity=_ident("bob", admin=False))] == ["acc_bob_slack"]


def test_nobody_can_disconnect_someone_elses_account(provider):
    from brain.wrappers.api import composio_disconnect
    with pytest.raises(HTTPException) as e:
        composio_disconnect("acc_bob_slack", identity=_ident("alice", admin=True))
    assert e.value.status_code == 404
    assert provider["disconnected"] == []


def test_a_user_can_disconnect_their_own_account(provider, monkeypatch):
    from brain.core import db_pg
    # the webhook clean-up talks to the database; it is best-effort and not under test here
    monkeypatch.setattr(db_pg, "connect", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    from brain.wrappers.api import composio_disconnect
    assert composio_disconnect("acc_alice_gmail", identity=_ident("alice", admin=False)) == {"ok": True}
    assert provider["disconnected"] == ["acc_alice_gmail"]
