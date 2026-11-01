"""
Private connector data (one person's mailbox, calendar, repositories) must be
readable by its owner only. These tests cover every path that previously let it
leak to teammates or admins: the facts list, the search ledger, and the account
export. Search itself is covered by tests/test_privacy.py and tests/test_isolation.py.
"""
import json
import unittest.mock as mock
import uuid

import pytest

from tests.conftest import POSTGRES_AVAILABLE

pg = pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")


def _identity(api, ws, user, admin=False, team="t"):
    return api.Identity(user_id=user, email=f"{user}@x.example", workspace_id=ws, team_id=team,
                        principals=[user, "everyone"], role="admin" if admin else "member",
                        is_admin=admin)


def _fact(conn, ws, summary, visibility="team", owner=None, team="t"):
    from brain.core import db
    fid = str(uuid.uuid4())
    db.save_fact(conn, ws, {
        "id": fid, "subject": "s", "predicate": "p", "object": "o", "summary": summary,
        "episode_id": None, "acl": "everyone", "confidence": 0.9,
        "introduced_at": "2026-01-01T00:00:00", "source_kind": "human", "team_id": team,
        "visibility": visibility, "owner_id": owner}, [0.1] * 768)
    return fid


@pytest.fixture
def client_as(ws_id, pg_conn):
    """TestClient whose caller identity can be chosen per request."""
    from fastapi.testclient import TestClient
    from brain.wrappers import api
    from brain.core import db
    db.migrate(pg_conn)
    holder = {}
    api.app.dependency_overrides[api.get_identity] = lambda: holder["who"]

    def make(user, admin=False, team="t"):
        holder["who"] = _identity(api, ws_id, user, admin=admin, team=team)
        return TestClient(api.app, raise_server_exceptions=False)

    yield make
    api.app.dependency_overrides.pop(api.get_identity, None)


# ── the facts list ───────────────────────────────────────────────────────────

@pg
def test_facts_list_hides_a_teammates_private_facts(client_as, pg_conn, ws_id):
    _fact(pg_conn, ws_id, "Team decision: we use Postgres.")
    _fact(pg_conn, ws_id, "ALICE PRIVATE: dentist on Friday", visibility="private", owner="alice")
    bob = client_as("bob").get("/api/facts").json()
    texts = " ".join(f["summary"] for f in bob)
    assert "Team decision" in texts and "ALICE PRIVATE" not in texts


@pg
def test_owner_still_sees_their_own_private_facts(client_as, pg_conn, ws_id):
    _fact(pg_conn, ws_id, "ALICE PRIVATE: dentist on Friday", visibility="private", owner="alice")
    alice = client_as("alice").get("/api/facts").json()
    assert any("ALICE PRIVATE" in f["summary"] for f in alice)


@pg
def test_an_admin_does_not_get_other_peoples_private_facts_even_cross_team(client_as, pg_conn, ws_id):
    _fact(pg_conn, ws_id, "ALICE PRIVATE: dentist on Friday", visibility="private", owner="alice")
    _fact(pg_conn, ws_id, "Shared roadmap fact.", visibility="shared", team="other")
    rows = client_as("root", admin=True).get("/api/facts?cross_team=true").json()
    texts = " ".join(f["summary"] for f in rows)
    assert "Shared roadmap" in texts and "ALICE PRIVATE" not in texts


@pg
def test_facts_list_does_not_send_the_embedding_vector(client_as, pg_conn, ws_id):
    _fact(pg_conn, ws_id, "A plain team fact.")
    rows = client_as("bob").get("/api/facts").json()
    assert rows and "fact_vec_768" not in rows[0] and "fts_vec" not in rows[0]


# ── the ledger ───────────────────────────────────────────────────────────────

def test_search_never_copies_private_fact_text_into_the_ledger():
    from brain.core import retrieve
    rows = [
        {"id": "p1", "summary": "ALICE PRIVATE: mortgage ref 998877", "visibility": "private",
         "acl": "everyone", "team_id": "t", "owner_id": "alice", "invalidated_at": None, "confidence": 0.9},
        {"id": "t1", "summary": "Team fact: we use Postgres.", "visibility": "team",
         "acl": "everyone", "team_id": "t", "owner_id": None, "invalidated_at": None, "confidence": 0.9},
    ]
    conn = mock.MagicMock()
    conn.cursor.return_value.fetchall.return_value = rows
    logged = {}
    with mock.patch.object(retrieve, "_cached_embed", return_value=[0.1] * 768), \
         mock.patch.object(retrieve, "_cache_get", return_value=None), \
         mock.patch.object(retrieve, "_cache_set"), \
         mock.patch.object(retrieve, "_vec", return_value=["p1", "t1"]), \
         mock.patch.object(retrieve, "_fts", return_value=[]), \
         mock.patch.object(retrieve.db, "log", side_effect=lambda *a, **k: logged.update(args=a)):
        out = retrieve.run(conn, "ws", "what is the mortgage", ["alice", "everyone"], "alice", "web", team_id="t")
    assert {f["id"] for f in out} == {"p1", "t1"}            # the owner still gets her own fact
    ids, context = logged["args"][5], logged["args"][6]
    assert "p1" in ids                                        # the id is kept for audit
    assert "998877" not in context and "ALICE PRIVATE" not in context   # the text is not
    assert "Team fact" in context


@pg
def test_ledger_endpoint_does_not_return_context_text(client_as, pg_conn, ws_id):
    from brain.core import db
    db.log(pg_conn, ws_id, "alice", "q", "web", ["x"], "SECRET CONTEXT TEXT", team_id="t")
    rows = client_as("bob").get("/api/ledger").json()
    assert rows and all("context_sent" not in r for r in rows)
    assert "SECRET CONTEXT TEXT" not in json.dumps(rows)


# ── the account export ───────────────────────────────────────────────────────

@pg
def test_admin_export_excludes_private_episodes_facts_and_edges(client_as, pg_conn, ws_id):
    from brain.core import db
    public = _fact(pg_conn, ws_id, "Public team fact.")
    private = _fact(pg_conn, ws_id, "ALICE PRIVATE: dentist", visibility="private", owner="alice")
    db.add_edge(pg_conn, ws_id, public, private, "in_tension_with")
    cur = pg_conn.cursor()
    for vis, body in (("team", "A normal shared note."), ("private", "ALICE PRIVATE EMAIL BODY")):
        cur.execute(
            "INSERT INTO episodes (id, workspace_id, source, external_id, title, body, content_hash, "
            "team_id, visibility, owner_id) VALUES (%s,%s,'composio:gmail',%s,'t',%s,%s,'t',%s,%s)",
            (str(uuid.uuid4()), ws_id, str(uuid.uuid4()), body, str(uuid.uuid4()), vis,
             "alice" if vis == "private" else None))
    pg_conn.commit()
    r = client_as("root", admin=True).get("/api/account/export")
    assert r.status_code == 200
    body = r.text
    assert "Public team fact" in body and "A normal shared note" in body
    assert "ALICE PRIVATE" not in body
    assert json.loads(body)["fact_edges"] == []                # an edge touching a private fact is excluded
