"""Semantic reconciliation: embeddings find candidates, a judge classifies them.

The judge is stubbed so these tests need no LLM key or quota. Embeddings are
hand-built unit vectors so closeness is exact and deterministic.
"""
import math
import uuid
import unittest.mock as mock

import pytest

from brain.core import reconcile

DIM = 768


def vec(*head):
    """A unit vector with the given leading components, zero elsewhere."""
    v = list(head) + [0.0] * (DIM - len(head))
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _cand(similarity, subject="Launch", cid=None):
    return {"id": cid or str(uuid.uuid4()), "subject": subject,
            "summary": "The launch is July 5th.", "similarity": similarity}


class TestReconcileDecisions:
    def test_no_candidates_means_nothing_to_do(self):
        with mock.patch.object(reconcile, "find_related", return_value=[]):
            d = reconcile.reconcile(None, "ws", {"summary": "x"}, vec(1), "eng")
        assert d.duplicate_of is None and not d.supersedes and not d.in_tension

    def test_judge_verdicts_are_mapped(self):
        a, b, c, u = _cand(0.9), _cand(0.85), _cand(0.8), _cand(0.78)
        verdicts = {a["id"]: "duplicate", b["id"]: "supersedes",
                    c["id"]: "contradicts", u["id"]: "unrelated"}
        with mock.patch.object(reconcile, "find_related", return_value=[a, b, c, u]), \
             mock.patch.object(reconcile, "judge", return_value=verdicts):
            d = reconcile.reconcile(None, "ws", {"summary": "x"}, vec(1), "eng")
        assert d.duplicate_of == a["id"]
        assert d.supersedes == [b["id"]]
        assert d.in_tension == [c["id"]]

    def test_unknown_candidate_defaults_to_unrelated(self):
        a = _cand(0.9)
        with mock.patch.object(reconcile, "find_related", return_value=[a]), \
             mock.patch.object(reconcile, "judge", return_value={}):
            d = reconcile.reconcile(None, "ws", {"summary": "x"}, vec(1), "eng")
        assert d.duplicate_of is None and not d.supersedes and not d.in_tension

    def test_judge_down_never_supersedes_on_similarity_alone(self):
        close = _cand(0.99, subject="The launch")
        with mock.patch.object(reconcile, "find_related", return_value=[close]), \
             mock.patch.object(reconcile, "judge", return_value=None):
            d = reconcile.reconcile(None, "ws",
                                    {"summary": "x", "subject": "Launch"}, vec(1), "eng")
        assert not d.supersedes and not d.in_tension and d.duplicate_of is None

    def test_judge_down_keeps_legacy_same_subject_dedup(self):
        same = _cand(0.97, subject="Launch")
        with mock.patch.object(reconcile, "find_related", return_value=[same]), \
             mock.patch.object(reconcile, "judge", return_value=None):
            d = reconcile.reconcile(None, "ws",
                                    {"summary": "x", "subject": "Launch"}, vec(1), "eng")
        assert d.duplicate_of == same["id"]


class TestJudge:
    def test_unavailable_model_returns_none(self):
        # chat_json returns {"facts": [], "terms": []} when the LLM is down.
        with mock.patch.object(reconcile.llm, "chat_json",
                               return_value={"facts": [], "terms": []}):
            assert reconcile.judge("new", [_cand(0.9)]) is None

    def test_ignores_ids_and_relations_it_was_not_offered(self):
        c = _cand(0.9)
        raw = {"verdicts": [
            {"id": c["id"], "relation": "supersedes"},
            {"id": "not-a-candidate", "relation": "supersedes"},
            {"id": c["id"] + "x", "relation": "duplicate"},
        ]}
        with mock.patch.object(reconcile.llm, "chat_json", return_value=raw):
            assert reconcile.judge("new", [c]) == {c["id"]: "supersedes"}

    def test_rejects_invented_relation(self):
        c = _cand(0.9)
        raw = {"verdicts": [{"id": c["id"], "relation": "delete_everything"}]}
        with mock.patch.object(reconcile.llm, "chat_json", return_value=raw):
            assert reconcile.judge("new", [c]) == {}


class TestApply:
    def test_supersede_invalidates_old_and_links(self):
        d = reconcile.Decision(supersedes=["old1"], in_tension=["other"])
        with mock.patch.object(reconcile.db, "invalidate_fact") as inv, \
             mock.patch.object(reconcile.db, "add_edge") as edge:
            reconcile.apply(None, "ws", "new", d, "2026-01-01")
        inv.assert_called_once_with(None, "ws", "old1", "2026-01-01")
        assert edge.call_args_list == [
            mock.call(None, "ws", "new", "old1", "supersedes"),
            mock.call(None, "ws", "new", "other", "in_tension_with"),
        ]


try:
    from tests.conftest import POSTGRES_AVAILABLE
except Exception:  # pragma: no cover
    POSTGRES_AVAILABLE = False


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
class TestFindRelatedAgainstPostgres:
    """Real pgvector: scoping and the similarity floor."""

    def _insert(self, conn, ws, emb, team="eng", visibility="team", owner=None,
                subject="Launch", summary="The launch is July 5th."):
        from brain.core import db
        db.save_fact(conn, ws, {
            "id": str(uuid.uuid4()), "subject": subject, "predicate": "is",
            "object": "x", "summary": summary, "episode_id": None, "acl": "everyone",
            "confidence": 0.9, "introduced_at": "2026-01-01T00:00:00",
            "source_kind": "human", "team_id": team, "visibility": visibility,
            "owner_id": owner,
        }, emb)

    def test_only_close_same_team_facts_are_candidates(self, pg_conn, ws_id):
        from brain.core import db
        db.migrate(pg_conn)
        self._insert(pg_conn, ws_id, vec(1, 0))              # same meaning
        self._insert(pg_conn, ws_id, vec(0, 1))              # orthogonal -> below floor
        self._insert(pg_conn, ws_id, vec(1, 0), team="sales")  # other team
        got = reconcile.find_related(pg_conn, ws_id, vec(1, 0.05), "eng")
        assert len(got) == 1 and got[0]["similarity"] > 0.99

    def test_private_fact_hidden_from_other_owner(self, pg_conn, ws_id):
        from brain.core import db
        db.migrate(pg_conn)
        self._insert(pg_conn, ws_id, vec(1, 0), visibility="private", owner="alice")
        assert reconcile.find_related(pg_conn, ws_id, vec(1, 0), "eng", owner_id="bob") == []
        assert len(reconcile.find_related(pg_conn, ws_id, vec(1, 0), "eng", owner_id="alice")) == 1


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
def test_history_search_can_reach_superseded_facts(pg_conn, ws_id):
    """A superseded fact must be a retrieval candidate when history is requested."""
    from brain.core import db
    db.migrate(pg_conn)
    t = TestFindRelatedAgainstPostgres()
    t._insert(pg_conn, ws_id, vec(1, 0))
    fid = db.search_vec(pg_conn, vec(1, 0), 5, workspace_id=ws_id)[0]
    db.invalidate_fact(pg_conn, ws_id, fid, "2026-02-01T00:00:00")
    assert db.search_vec(pg_conn, vec(1, 0), 5, workspace_id=ws_id) == []
    assert db.search_vec(pg_conn, vec(1, 0), 5, workspace_id=ws_id, include_invalid=True) == [fid]
