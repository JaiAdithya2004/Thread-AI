"""
Regression tests for defects the end-to-end benchmark found in the product:

  * a null field from the extraction model discarded the whole episode;
  * "how has X changed?" could only see the latest value, because search hides
    superseded facts;
  * the answer model was not told today's date or which facts were superseded.
"""
import unittest.mock as mock

import pytest


# ── extraction tolerates nulls ───────────────────────────────────────────────

def test_null_object_does_not_discard_the_fact():
    from brain.core.extract import ExtractionResult
    r = ExtractionResult(**{"facts": [{"subject": "Pro plan", "predicate": "costs", "object": None,
                                       "summary": "The Pro plan costs $29 per month.", "confidence": 0.9}]})
    assert len(r.facts) == 1 and r.facts[0].object == "" and r.facts[0].summary.startswith("The Pro plan")


def test_null_predicate_subject_and_confidence_get_defaults():
    from brain.core.extract import ExtractionResult
    f = ExtractionResult(**{"facts": [{"subject": None, "predicate": None, "object": None,
                                       "summary": "A durable fact.", "confidence": None}]}).facts[0]
    assert (f.subject, f.predicate, f.object) == ("", "", "") and f.confidence == 0.6


def test_one_malformed_fact_keeps_the_others():
    from brain.core.extract import ExtractionResult
    r = ExtractionResult(**{"facts": [
        {"summary": "Good fact one."}, {"summary": ""}, "not a dict", {"object": "no summary"},
        {"summary": "Good fact two."}]})
    assert [f.summary for f in r.facts] == ["Good fact one.", "Good fact two."]


def test_non_list_facts_becomes_empty_not_an_error():
    from brain.core.extract import ExtractionResult
    assert ExtractionResult(**{"facts": "oops"}).facts == []


# ── history intent ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "How has the Standard plan first-response SLA changed over time?",
    "Who has owned the billing service over time?",
    "What has the mobile app been built with?" if False else "How have prices changed?",
    "What did the launch date used to be?",
    "Show the timeline of the pricing decision",
    "What was it previously?",
])
def test_history_questions_are_recognised(q):
    from brain.core.retrieve import _wants_history
    assert _wants_history(q)


@pytest.mark.parametrize("q", [
    "What is the current monthly price of the Pro plan?",
    "Who owns the billing service?",
    "Where is customer data stored by default?",
])
def test_current_and_direct_questions_are_not_history(q):
    from brain.core.retrieve import _wants_history
    assert not _wants_history(q)


def test_history_question_asks_retrieval_to_include_superseded_facts():
    from brain.core import retrieve
    captured = {}

    def fake_vec(conn, emb, n=30, workspace_id=None, include_invalid=False):
        captured["include_invalid"] = include_invalid
        return []

    with mock.patch.object(retrieve, "_cached_embed", return_value=[0.1] * 768), \
         mock.patch.object(retrieve, "_cache_get", return_value=None), \
         mock.patch.object(retrieve, "_cache_set"), \
         mock.patch.object(retrieve, "_vec", side_effect=fake_vec), \
         mock.patch.object(retrieve, "_fts", return_value=[]), \
         mock.patch.object(retrieve.db, "log"):
        retrieve.run(mock.MagicMock(), "ws", "How has the price changed over time?", ["everyone"], "u", "test")
        assert captured["include_invalid"] is True
        retrieve.run(mock.MagicMock(), "ws", "What is the price?", ["everyone"], "u", "test")
        assert captured["include_invalid"] is False


# ── what the answer model is told ────────────────────────────────────────────

def _capture_prompt(facts, query="q"):
    from brain.core import retrieve
    seen = {}

    def fake_chat(system, user, model=None, task_name=None, **kw):
        seen["system"], seen["user"] = system, user
        return "ok"

    with mock.patch.object(retrieve.llm, "chat_text", side_effect=fake_chat):
        retrieve.synthesize_answer(query, facts)
    return seen


def test_answer_prompt_labels_superseded_facts_and_gives_dates():
    seen = _capture_prompt([
        {"fact_id": "a1", "summary": "The Pro plan costs $29.", "status": "stale", "as_of": "2026-01-05"},
        {"fact_id": "b2", "summary": "The Pro plan costs $35.", "status": "valid", "as_of": "2026-03-12"},
        {"fact_id": "c3", "summary": "Team plan is $79.", "status": "valid", "as_of": None},
    ])
    u = seen["user"]
    assert "[a1] (as of 2026-01-05; superseded) The Pro plan costs $29." in u
    assert "[b2] (as of 2026-03-12) The Pro plan costs $35." in u
    assert "[c3] Team plan is $79." in u


def test_answer_prompt_states_todays_date_and_how_to_use_superseded_facts():
    import datetime
    seen = _capture_prompt([{"fact_id": "a1", "summary": "x", "status": "valid"}])
    s = seen["system"]
    assert datetime.date.today().isoformat() in s
    assert "superseded" in s and "future" in s
    # the security rules must still be present and still outrank everything
    assert "SECURITY RULES" in s and "<untrusted_content>" in seen["user"]


def test_cite_carries_the_event_date_when_known():
    from brain.core.retrieve import _cite
    base = {"summary": "s", "confidence": 0.9, "source_kind": "llm", "id": "f1", "invalidated_at": None}
    assert _cite(dict(base, ep_occurred_at="2026-03-12T09:45:00+00:00"))["as_of"] == "2026-03-12"
    assert _cite(dict(base, introduced_at="2026-04-01T00:00:00"))["as_of"] == "2026-04-01"
    assert _cite(base)["as_of"] is None


# ── a message cannot supersede itself ────────────────────────────────────────

def _cand(cid, episode_id):
    return {"id": cid, "subject": "s", "summary": f"fact {cid}", "similarity": 0.9, "episode_id": episode_id}


def test_facts_from_the_same_message_do_not_retire_each_other():
    from brain.core import reconcile
    cands = [_cand("sibling", "ep-1"), _cand("other-message", "ep-0")]
    verdicts = {"sibling": "supersedes", "other-message": "supersedes"}
    with mock.patch.object(reconcile, "find_related", return_value=cands), \
         mock.patch.object(reconcile, "judge", return_value=verdicts):
        d = reconcile.reconcile(mock.MagicMock(), "ws", {"summary": "x"}, [1.0], "t", episode_id="ep-1")
    assert d.supersedes == ["other-message"]          # the sibling is left alone


def test_same_message_conflicts_are_ignored_too():
    from brain.core import reconcile
    with mock.patch.object(reconcile, "find_related", return_value=[_cand("sibling", "ep-1")]), \
         mock.patch.object(reconcile, "judge", return_value={"sibling": "contradicts"}):
        d = reconcile.reconcile(mock.MagicMock(), "ws", {"summary": "x"}, [1.0], "t", episode_id="ep-1")
    assert not d.in_tension and not d.supersedes


def test_a_duplicate_within_the_same_message_is_still_caught():
    from brain.core import reconcile
    with mock.patch.object(reconcile, "find_related", return_value=[_cand("sibling", "ep-1")]), \
         mock.patch.object(reconcile, "judge", return_value={"sibling": "duplicate"}):
        d = reconcile.reconcile(mock.MagicMock(), "ws", {"summary": "x"}, [1.0], "t", episode_id="ep-1")
    assert d.duplicate_of == "sibling"


def test_without_an_episode_id_behaviour_is_unchanged():
    from brain.core import reconcile
    with mock.patch.object(reconcile, "find_related", return_value=[_cand("a", "ep-1")]), \
         mock.patch.object(reconcile, "judge", return_value={"a": "supersedes"}):
        d = reconcile.reconcile(mock.MagicMock(), "ws", {"summary": "x"}, [1.0], "t")
    assert d.supersedes == ["a"]


def test_benchmark_judge_sees_the_same_labels_and_date_as_the_answerer():
    import datetime
    from evals import bench
    seen = {}

    def fake(system, user, model=None, task_name=None, **kw):
        seen["system"], seen["user"] = system, user
        return {"faithful": True}

    with mock.patch("brain.core.llm.chat_json", side_effect=fake):
        bench.judge_faithful("q", "a", [
            {"summary": "old", "status": "stale", "as_of": "2026-01-01"},
            {"summary": "new", "status": "valid", "as_of": "2026-03-01"}])
    assert datetime.date.today().isoformat() in seen["user"]
    assert "(as of 2026-01-01; superseded) old" in seen["user"] and "(as of 2026-03-01) new" in seen["user"]
