"""
Every signed-in account gets a workspace of its own, resolved on the server.

Regression: a new sign-up used to inherit one deployment-wide default workspace,
which in production was the public demo's. They saw demo data, could not do
anything (the demo guard blocks writes), and were not separated from each other.
These tests need no database: the account lookup is stubbed.
"""
import pytest

from brain.auth import GUEST_WORKSPACE


@pytest.fixture
def auth(monkeypatch):
    import brain.auth as a
    a._WS_CACHE.clear()
    a._ROLE_CACHE.clear()
    monkeypatch.setattr(a, "_lookup_account_workspace", lambda uid: None)
    monkeypatch.setattr(a, "_lookup_membership_role", lambda uid: None)
    return a


def _payload(sub, **meta):
    return {"sub": sub, "email": f"{sub}@example.com", "public_metadata": meta}


def test_new_user_without_account_gets_a_private_workspace_not_the_demo(auth):
    ident = auth._identity_from_clerk_payload(_payload("user_new"))
    assert ident.workspace_id != GUEST_WORKSPACE
    assert ident.workspace_id.startswith("ws_u_")
    assert ident.is_guest is False


def test_two_users_never_share_a_workspace(auth):
    a = auth._identity_from_clerk_payload(_payload("user_a"))
    b = auth._identity_from_clerk_payload(_payload("user_b"))
    assert a.workspace_id != b.workspace_id


def test_workspace_is_stable_for_the_same_user(auth):
    first = auth._identity_from_clerk_payload(_payload("user_a")).workspace_id
    again = auth._identity_from_clerk_payload(_payload("user_a")).workspace_id
    assert first == again


def test_account_workspace_is_used_when_the_user_has_an_account(auth, monkeypatch):
    monkeypatch.setattr(auth, "_lookup_account_workspace", lambda uid: "ws_acct123")
    ident = auth._identity_from_clerk_payload(_payload("user_a"))
    assert ident.workspace_id == "ws_acct123"


def test_members_of_one_account_share_its_workspace(auth, monkeypatch):
    monkeypatch.setattr(auth, "_lookup_account_workspace", lambda uid: "ws_team1")
    a = auth._identity_from_clerk_payload(_payload("alice"))
    b = auth._identity_from_clerk_payload(_payload("bob"))
    assert a.workspace_id == b.workspace_id == "ws_team1"


def test_a_signed_in_user_can_never_be_placed_in_the_demo_workspace(auth, monkeypatch):
    # the account lookup returning the demo workspace
    monkeypatch.setattr(auth, "_lookup_account_workspace", lambda uid: GUEST_WORKSPACE)
    assert auth._identity_from_clerk_payload(_payload("user_a")).workspace_id != GUEST_WORKSPACE
    # token metadata naming the demo workspace
    auth._WS_CACHE.clear()
    ident = auth._identity_from_clerk_payload(_payload("user_b", workspace_id=GUEST_WORKSPACE))
    assert ident.workspace_id != GUEST_WORKSPACE


def test_guests_are_still_locked_to_the_demo_workspace(auth):
    ident = auth._identity_from_clerk_payload(_payload("g1", role="guest", workspace_id="ws_other"))
    assert ident.workspace_id == GUEST_WORKSPACE
    assert ident.is_guest is True


def test_an_unreadable_database_falls_back_instead_of_failing_the_request(auth, monkeypatch):
    def boom(uid):
        raise RuntimeError("database unreachable")
    monkeypatch.setattr(auth, "_lookup_account_workspace", boom)
    ident = auth._identity_from_clerk_payload(_payload("user_a"))
    assert ident.workspace_id.startswith("ws_u_")


def test_the_fallback_is_not_cached_so_the_real_workspace_takes_over(auth, monkeypatch):
    first = auth._identity_from_clerk_payload(_payload("user_a")).workspace_id
    assert first.startswith("ws_u_")
    monkeypatch.setattr(auth, "_lookup_account_workspace", lambda uid: "ws_real")
    assert auth._identity_from_clerk_payload(_payload("user_a")).workspace_id == "ws_real"


def test_the_account_lookup_is_cached(auth, monkeypatch):
    calls = []
    def lookup(uid):
        calls.append(uid)
        return "ws_acct"
    monkeypatch.setattr(auth, "_lookup_account_workspace", lookup)
    auth._identity_from_clerk_payload(_payload("user_a"))
    auth._identity_from_clerk_payload(_payload("user_a"))
    assert calls == ["user_a"]


# ── roles come from the account membership, not from a default ──────────────

def test_an_account_owner_is_an_admin_of_their_own_workspace(auth, monkeypatch):
    monkeypatch.setattr(auth, "_lookup_membership_role", lambda uid: "owner")
    ident = auth._identity_from_clerk_payload(_payload("user_a"))
    assert ident.role == "owner" and ident.is_admin is True


def test_a_company_admin_membership_makes_an_admin(auth, monkeypatch):
    monkeypatch.setattr(auth, "_lookup_membership_role", lambda uid: "admin")
    assert auth._identity_from_clerk_payload(_payload("user_a")).is_admin is True


def test_an_invited_employee_is_a_plain_member(auth, monkeypatch):
    monkeypatch.setattr(auth, "_lookup_membership_role", lambda uid: "employee")
    ident = auth._identity_from_clerk_payload(_payload("user_a"))
    assert ident.role == "member" and ident.is_admin is False


def test_no_membership_means_member_not_admin(auth):
    assert auth._identity_from_clerk_payload(_payload("user_a")).is_admin is False


def test_a_role_set_on_the_clerk_user_still_wins(auth, monkeypatch):
    monkeypatch.setattr(auth, "_lookup_membership_role", lambda uid: "owner")
    ident = auth._identity_from_clerk_payload(_payload("user_a", role="member"))
    assert ident.role == "member" and ident.is_admin is False


def test_an_unreadable_database_never_grants_admin(auth, monkeypatch):
    def boom(uid):
        raise RuntimeError("database unreachable")
    monkeypatch.setattr(auth, "_lookup_membership_role", boom)
    assert auth._identity_from_clerk_payload(_payload("user_a")).is_admin is False


# ── the database lookup itself, against a fake connection ────────────────────

class _Cur:
    def __init__(self, rows):
        self._rows, self.executed = rows, []
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
    def fetchone(self):
        return self._rows.pop(0) if self._rows else None
    def close(self):
        pass


class _Conn:
    def __init__(self, rows):
        self.cur, self.commits = _Cur(rows), 0
    def cursor(self):
        return self.cur
    def commit(self):
        self.commits += 1
    def close(self):
        pass


def test_user_with_no_membership_has_no_account_workspace(monkeypatch):
    from brain.core import db_pg
    conn = _Conn([None])
    monkeypatch.setattr(db_pg, "connect", lambda: conn)
    assert db_pg.workspace_for_user("nobody") is None


def test_existing_account_without_a_workspace_is_assigned_one_from_its_id(monkeypatch):
    from brain.core import db_pg
    conn = _Conn([{"account_id": "11111111-2222-3333-4444-555555555555", "workspace_id": None}])
    monkeypatch.setattr(db_pg, "connect", lambda: conn)
    ws = db_pg.workspace_for_user("u1")
    assert ws == "ws_11111111222233334444555555555555"
    assert any("UPDATE accounts SET workspace_id" in sql for sql, _ in conn.cur.executed)
    assert conn.commits == 1


def test_existing_workspace_is_returned_unchanged_and_not_rewritten(monkeypatch):
    from brain.core import db_pg
    conn = _Conn([{"account_id": "acct-1", "workspace_id": "ws_keepme"}])
    monkeypatch.setattr(db_pg, "connect", lambda: conn)
    assert db_pg.workspace_for_user("u1") == "ws_keepme"
    assert not any("UPDATE" in sql for sql, _ in conn.cur.executed)
    assert conn.commits == 0
