"""
Tests for the real-data validation tooling (evals/real_eval.py) and for
reconcile's shadow / off modes.

The scoring and audit logic is exercised with fake search functions and fake rows
so it needs no model and no database; the export paths run against Postgres when
DATABASE_URL is set.
"""
import csv
import json
import unittest.mock as mock
import uuid

import pytest

from tests.conftest import POSTGRES_AVAILABLE


# ── redaction ────────────────────────────────────────────────────────────────

def test_redact_masks_emails_urls_and_long_numbers():
    from evals.real_eval import redact
    out = redact("Mail jo.smith@corp.example or see https://x.example/a?b=1 ref 12345678 and 42")
    assert "corp.example" not in out and "x.example" not in out and "12345678" not in out
    assert "<email>" in out and "<url>" in out and "<number>" in out
    assert "42" in out                       # short numbers are meaningful and kept


# ── scoring a labelled set ───────────────────────────────────────────────────

def _res(*ids, text="", answer="From company memory:"):
    return {"facts": [{"fact_id": i, "summary": f"fact {i}"} for i in ids],
            "synthesized_answer": text, "answer": answer}


def test_score_labelled_counts_hits_and_misses():
    from evals.real_eval import score_labelled
    entries = [
        {"id": "a", "question": "q1", "gold": ["f1"]},                    # hit at rank 1
        {"id": "b", "question": "q2", "gold": ["f9"]},                    # miss
        {"id": "c", "question": "q3", "gold": ["f3"]},                    # hit at rank 2
    ]
    results = {"q1": _res("f1", "f2"), "q2": _res("f2", "f3"), "q3": _res("f2", "f3")}
    out = score_labelled(entries, lambda q: results[q])
    assert out["n_labelled_answerable"] == 3
    assert out["retrieval"]["hit@k"]["k"] == 2 and out["retrieval"]["hit@k"]["n"] == 3
    assert out["retrieval"]["mrr"] == pytest.approx((1 + 0 + 0.5) / 3, abs=1e-3)
    lo, hi = out["retrieval"]["hit@k"]["ci95"]
    assert lo < 2 / 3 < hi                    # small samples get wide intervals


def test_unlabelled_answerable_questions_are_skipped_not_scored_as_misses():
    from evals.real_eval import score_labelled
    entries = [{"id": "a", "question": "q1", "gold": []},
               {"id": "b", "question": "q2", "gold": ["f1"]}]
    out = score_labelled(entries, lambda q: _res("f1"))
    assert out["n_skipped_unlabelled"] == 1 and out["n_labelled_answerable"] == 1


def test_abstention_is_scored_on_unanswerable_questions():
    from evals.real_eval import score_labelled
    entries = [
        {"id": "a", "question": "refuses", "answerable": False},
        {"id": "b", "question": "bluffs", "answerable": False},
        {"id": "c", "question": "nothing found", "answerable": False},
    ]
    results = {
        "refuses": _res("f1", text="I don't have that information in company memory."),
        "bluffs": _res("f1", text="The answer is 42 [f1]."),
        "nothing found": _res(),
    }
    out = score_labelled(entries, lambda q: results[q])
    assert out["abstention"]["k"] == 2 and out["abstention"]["n"] == 3


# ── auditing supersession decisions ──────────────────────────────────────────

def _rows(verdicts, kind="supersedes"):
    return [{"edge_id": i, "type": kind, "newer_fact": "n", "older_fact": "o",
             "verdict": v, "notes": ""} for i, v in enumerate(verdicts)]


def test_audit_precision_ignores_unsure_and_unreviewed():
    from evals.real_eval import score_audit
    res = score_audit(_rows(["correct"] * 9 + ["wrong", "unsure", ""]))
    o = res["overall"]
    assert (o["correct"], o["wrong"], o["unsure"], o["unreviewed"]) == (9, 1, 1, 1)
    assert o["precision"]["value"] == pytest.approx(0.9)


def test_audit_scores_each_decision_type_separately():
    from evals.real_eval import score_audit
    rows = _rows(["correct", "correct"], "supersedes") + _rows(["wrong"], "in_tension_with")
    res = score_audit(rows)
    assert res["supersedes"]["precision"]["value"] == 1.0
    assert res["in_tension_with"]["precision"]["value"] == 0.0


def test_audit_score_command_reads_a_reviewed_csv(tmp_path, capsys):
    from evals.real_eval import main, AUDIT_COLUMNS
    f = tmp_path / "audit.csv"
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=AUDIT_COLUMNS)
        w.writeheader()
        w.writerows(_rows(["correct"] * 98 + ["wrong"] * 2))
    assert main(["audit-score", "--file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "0.980" in out and "95% CI" in out


# ── reconcile modes ──────────────────────────────────────────────────────────

def _candidates(n=1):
    return [{"id": f"c{i}", "subject": "s", "summary": f"old {i}", "similarity": 0.9} for i in range(n)]


def test_off_mode_does_nothing_at_all():
    from brain.core import reconcile
    with mock.patch.object(reconcile.config, "RECONCILE_MODE", "off"), \
         mock.patch.object(reconcile, "find_related") as fr, \
         mock.patch.object(reconcile, "judge") as jd:
        d = reconcile.reconcile(None, "ws", {"summary": "x"}, [1.0], "t")
    assert d.duplicate_of is None and not d.supersedes
    fr.assert_not_called()
    jd.assert_not_called()


def test_shadow_mode_logs_the_decision_and_applies_nothing():
    from brain.core import reconcile
    conn = mock.MagicMock()
    with mock.patch.object(reconcile.config, "RECONCILE_MODE", "shadow"), \
         mock.patch.object(reconcile, "find_related", return_value=_candidates(2)), \
         mock.patch.object(reconcile, "judge", return_value={"c0": "supersedes", "c1": "duplicate"}):
        d = reconcile.reconcile(conn, "ws", {"summary": "new fact"}, [1.0], "team")
    assert d.duplicate_of is None and not d.supersedes and not d.in_tension   # nothing applied
    sql, params = conn.cursor.return_value.execute.call_args.args
    assert "reconcile_shadow_log" in sql
    logged = json.loads(params[4])
    assert logged["supersedes"] == ["c0"] and logged["duplicate_of"] == "c1"
    assert {c["id"]: c["verdict"] for c in json.loads(params[5])} == {"c0": "supersedes", "c1": "duplicate"}


def test_enforce_mode_is_unchanged():
    from brain.core import reconcile
    with mock.patch.object(reconcile.config, "RECONCILE_MODE", "enforce"), \
         mock.patch.object(reconcile, "find_related", return_value=_candidates(1)), \
         mock.patch.object(reconcile, "judge", return_value={"c0": "supersedes"}):
        d = reconcile.reconcile(mock.MagicMock(), "ws", {"summary": "x"}, [1.0], "t")
    assert d.supersedes == ["c0"]


def test_a_broken_shadow_log_never_breaks_ingestion():
    from brain.core import reconcile
    conn = mock.MagicMock()
    conn.cursor.return_value.execute.side_effect = RuntimeError("disk full")
    with mock.patch.object(reconcile.config, "RECONCILE_MODE", "shadow"), \
         mock.patch.object(reconcile, "find_related", return_value=_candidates(1)), \
         mock.patch.object(reconcile, "judge", return_value={"c0": "supersedes"}):
        d = reconcile.reconcile(conn, "ws", {"summary": "x"}, [1.0], "t")
    assert not d.supersedes


def test_unknown_mode_falls_back_to_enforce():
    from brain.core import reconcile
    with mock.patch.object(reconcile.config, "RECONCILE_MODE", "banana"):
        assert reconcile._mode() == "enforce"


# ── exports against a real database ──────────────────────────────────────────

pg = pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")


def _fact(conn, ws, summary, visibility="team", owner=None):
    from brain.core import db
    fid = str(uuid.uuid4())
    db.save_fact(conn, ws, {
        "id": fid, "subject": "s", "predicate": "p", "object": "o", "summary": summary,
        "episode_id": None, "acl": "everyone", "confidence": 0.9,
        "introduced_at": "2026-01-01T00:00:00", "source_kind": "human", "team_id": "t",
        "visibility": visibility, "owner_id": owner}, [0.1] * 768)
    return fid


@pg
def test_sample_exports_questions_but_never_private_facts(pg_conn, ws_id):
    from brain.core import db
    from evals.real_eval import sample_questions
    db.migrate(pg_conn)
    public = _fact(pg_conn, ws_id, "The Pro plan costs $35 per month.")
    private = _fact(pg_conn, ws_id, "My mortgage ref is 998877665", visibility="private", owner="alice")
    db.log(pg_conn, ws_id, "alice", "What does Pro cost? mail me@corp.example", "web",
           [public, private], "ctx", team_id="t")
    db.log(pg_conn, ws_id, "bob", "what does pro cost? mail me@corp.example", "web",
           [public], "ctx", team_id="t")                      # duplicate question
    rows = sample_questions(pg_conn, ws_id, "t", n=10, seed=1, do_redact=True)
    assert len(rows) == 1                                      # de-duplicated
    assert "corp.example" not in rows[0]["question"]
    returned = {r["id"] for r in rows[0]["returned_then"]}
    assert returned == {public}                                # the private fact is not exported
    assert rows[0]["gold"] == [] and rows[0]["answerable"] is True


@pg
def test_audit_sample_from_edges_skips_private_facts(pg_conn, ws_id):
    from brain.core import db
    from evals.real_eval import audit_rows_from_edges
    db.migrate(pg_conn)
    old, new = _fact(pg_conn, ws_id, "Price is $29."), _fact(pg_conn, ws_id, "Price is $35.")
    p_old = _fact(pg_conn, ws_id, "Secret old", visibility="private", owner="a")
    p_new = _fact(pg_conn, ws_id, "Secret new", visibility="private", owner="a")
    db.add_edge(pg_conn, ws_id, new, old, "supersedes")
    db.add_edge(pg_conn, ws_id, p_new, p_old, "supersedes")
    rows = audit_rows_from_edges(pg_conn, ws_id, n=10, seed=1, do_redact=False)
    assert [(r["newer_fact"], r["older_fact"]) for r in rows] == [("Price is $35.", "Price is $29.")]
    assert rows[0]["verdict"] == ""


@pg
def test_audit_sample_from_shadow_log(pg_conn, ws_id):
    from brain.core import db, reconcile
    from evals.real_eval import audit_rows_from_shadow
    db.migrate(pg_conn)
    with mock.patch.object(reconcile.config, "RECONCILE_MODE", "shadow"), \
         mock.patch.object(reconcile, "find_related",
                           return_value=[{"id": "c0", "subject": "s", "summary": "Price is $29.",
                                          "similarity": 0.9}]), \
         mock.patch.object(reconcile, "judge", return_value={"c0": "supersedes"}):
        reconcile.reconcile(pg_conn, ws_id, {"summary": "Price is $35."}, [1.0], "t")
    rows = audit_rows_from_shadow(pg_conn, ws_id, n=10, seed=1, do_redact=False)
    assert len(rows) == 1 and rows[0]["type"] == "would_supersede"
    assert rows[0]["newer_fact"] == "Price is $35." and rows[0]["older_fact"] == "Price is $29."
