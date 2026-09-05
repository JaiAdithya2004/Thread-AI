"""
Unit tests for brain/core/retrieve.py

Tests:
- RRF: results from vector + FTS are merged correctly
- ACL filter: fact with restricted acl not returned for wrong principals
- Confidence threshold filtering in format_answer
- format_answer graceful degradation (LLM down → returns facts without synthesis)
- format_history returns correct structure
"""
import os
import pytest

POSTGRES_AVAILABLE = bool(os.getenv("DATABASE_URL"))


# ── RRF ──────────────────────────────────────────────────────────────────────

def test_rrf_combines_vec_and_fts():
    """_rrf must merge two ranked lists, scoring items that appear in both higher."""
    from brain.core.retrieve import _rrf

    vec_results = ["fact-a", "fact-b", "fact-c"]
    fts_results = ["fact-b", "fact-d", "fact-a"]

    merged = _rrf(vec_results, fts_results)

    # fact-a and fact-b appear in both lists → should rank above fact-c / fact-d
    assert "fact-a" in merged
    assert "fact-b" in merged
    # Items appearing in both lists score higher (2 × 1/(k+rank)) than single-list items
    top_two = set(merged[:2])
    assert "fact-a" in top_two and "fact-b" in top_two, (
        f"Items in both lists should be top-ranked, got top-2: {top_two}"
    )


def test_rrf_single_list_preserves_order():
    """With a single list _rrf preserves the original order."""
    from brain.core.retrieve import _rrf

    lst = ["x", "y", "z"]
    merged = _rrf(lst)
    assert merged == lst


def test_rrf_empty_inputs():
    """_rrf with empty lists returns an empty list."""
    from brain.core.retrieve import _rrf
    assert _rrf([], []) == []


def test_rrf_deduplicates_ids():
    """_rrf must never return the same fact ID twice."""
    from brain.core.retrieve import _rrf

    a = ["id1", "id2", "id3"]
    b = ["id2", "id3", "id4"]
    merged = _rrf(a, b)
    assert len(merged) == len(set(merged)), "Duplicate IDs in RRF output"


# ── ACL filter ────────────────────────────────────────────────────────────────

def test_allowed_everyone_acl():
    """ACL='everyone' must be accessible to any principals list."""
    from brain.core.retrieve import _allowed
    assert _allowed("everyone", ["user-a@corp.com"])


def test_allowed_specific_principal():
    """ACL='user@corp.com' must be accessible when that principal is in the list."""
    from brain.core.retrieve import _allowed
    assert _allowed("user@corp.com", ["user@corp.com", "everyone"])


def test_allowed_blocks_wrong_principal():
    """ACL='team-x' must NOT be accessible to principals that don't include 'team-x'."""
    from brain.core.retrieve import _allowed
    assert not _allowed("team-x", ["team-y", "other-user"])


def test_allowed_blocks_no_overlap():
    """If no principal matches the ACL, access is denied."""
    from brain.core.retrieve import _allowed
    assert not _allowed("user-secret@corp.com", ["user-other@corp.com", "everyone"])


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
def test_retrieve_run_acl_filter(pg_conn, ws_id):
    """Facts with a restricted ACL must not appear in retrieve.run results for wrong principal."""
    from brain.core import retrieve, db, extract

    db.migrate(pg_conn)

    # Write a fact visible only to 'privileged@corp.com'
    extract.record_human_fact(
        pg_conn, ws_id,
        "Confidential: the board approved a $10M Series A.",
        acl=["privileged@corp.com"],   # NOT 'everyone'
        team_id="eng",
    )

    facts = retrieve.run(
        pg_conn, ws_id,
        "board approved Series A",
        principals=["other-user@corp.com", "everyone"],
        caller="other-user@corp.com",
        surface="test",
        team_id="eng",
    )
    for f in facts:
        acl = f.get("acl", "everyone")
        assert "privileged@corp.com" not in acl or "everyone" in acl, (
            f"ACL-restricted fact leaked: {f.get('summary')}"
        )


# ── Confidence threshold in format_answer ─────────────────────────────────────

def test_format_answer_low_confidence_returns_not_sure():
    """format_answer must return the 'not sure' path when top fact confidence < threshold."""
    from brain.core.retrieve import format_answer
    from brain.config import CONFIDENCE_THRESHOLD

    low_conf_facts = [
        {
            "id": "f1", "summary": "We might use Python",
            "confidence": CONFIDENCE_THRESHOLD - 0.01,
            "invalidated_at": None,
            "source_kind": "llm",
            "team_id": "eng",
            "visibility": "team",
            "episode_id": None,
        }
    ]
    result = format_answer("What language do we use?", low_conf_facts)
    assert "don't have reliable information" in result["answer"].lower() or \
           result["synthesized_answer"] == ""


def test_format_answer_empty_facts_returns_not_sure():
    """format_answer with no facts returns the 'not sure' path."""
    from brain.core.retrieve import format_answer
    result = format_answer("What is our pricing?", [])
    assert "don't have reliable information" in result["answer"].lower()
    assert result["facts"] == []


def test_format_answer_all_stale_returns_not_sure():
    """format_answer when all facts are invalidated returns the 'not sure' path."""
    from brain.core.retrieve import format_answer

    stale_facts = [
        {
            "id": "f1", "summary": "We used Ruby",
            "confidence": 0.9,
            "invalidated_at": "2026-01-01T00:00:00",  # stale
            "source_kind": "llm",
            "team_id": "eng",
            "visibility": "team",
            "episode_id": None,
        }
    ]
    result = format_answer("What language do we use?", stale_facts)
    assert "don't have reliable information" in result["answer"].lower()


# ── format_answer graceful degradation (LLM down) ────────────────────────────

def test_format_answer_degrades_gracefully_when_llm_returns_empty(monkeypatch):
    """When synthesize_answer returns '', format_answer must still return cited facts."""
    from brain.core import retrieve
    from brain.config import CONFIDENCE_THRESHOLD

    monkeypatch.setattr(retrieve, "synthesize_answer", lambda q, facts: "")

    good_facts = [
        {
            "id": "f1", "summary": "We use Python for all services.",
            "confidence": CONFIDENCE_THRESHOLD + 0.1,
            "invalidated_at": None,
            "source_kind": "human",
            "team_id": "eng",
            "visibility": "team",
            "episode_id": None,
        }
    ]
    result = retrieve.format_answer("What language do we use?", good_facts)
    # Must not crash; must include the fact; synthesis may be empty
    assert result["facts"] is not None
    assert len(result["facts"]) > 0
    assert result["synthesized_answer"] == ""


def test_format_answer_with_synthesis(monkeypatch):
    """When LLM is available, synthesized_answer must be non-empty."""
    from brain.core import retrieve
    from brain.config import CONFIDENCE_THRESHOLD

    monkeypatch.setattr(
        retrieve, "synthesize_answer",
        lambda q, facts: "Python is the primary backend language."
    )

    good_facts = [
        {
            "id": "f1", "summary": "We use Python for all services.",
            "confidence": CONFIDENCE_THRESHOLD + 0.1,
            "invalidated_at": None,
            "source_kind": "human",
            "team_id": "eng",
            "visibility": "team",
            "episode_id": None,
        }
    ]
    result = retrieve.format_answer("What language do we use?", good_facts)
    assert result["synthesized_answer"] != ""
    assert "Python" in result["synthesized_answer"]


# ── format_history ────────────────────────────────────────────────────────────

def test_format_history_returns_correct_structure():
    """format_history must return a dict with 'subject' and 'timeline' keys."""
    from brain.core.retrieve import format_history

    facts = [
        {
            "id": "f1", "summary": "We chose Ruby",
            "confidence": 0.8,
            "invalidated_at": "2026-01-01T00:00:00",
            "source_kind": "llm",
            "team_id": "eng",
            "visibility": "team",
            "episode_id": None,
            "introduced_at": "2026-01-01T00:00:00",
        },
        {
            "id": "f2", "summary": "We chose Python",
            "confidence": 0.9,
            "invalidated_at": None,
            "source_kind": "human",
            "team_id": "eng",
            "visibility": "team",
            "episode_id": None,
            "introduced_at": "2026-06-01T00:00:00",
        },
    ]
    result = format_history("backend language", facts)
    assert "subject" in result
    assert result["subject"] == "backend language"
    assert "timeline" in result
    assert isinstance(result["timeline"], list)
    assert len(result["timeline"]) == 2


def test_format_history_sorted_by_introduced_at():
    """format_history must sort facts chronologically (oldest first)."""
    from brain.core.retrieve import format_history

    facts = [
        {
            "id": "f2", "summary": "Later fact",
            "confidence": 0.9, "invalidated_at": None,
            "source_kind": "human", "team_id": "eng",
            "visibility": "team", "episode_id": None,
            "introduced_at": "2026-06-01T00:00:00",
        },
        {
            "id": "f1", "summary": "Earlier fact",
            "confidence": 0.8, "invalidated_at": None,
            "source_kind": "llm", "team_id": "eng",
            "visibility": "team", "episode_id": None,
            "introduced_at": "2026-01-01T00:00:00",
        },
    ]
    result = format_history("backend language", facts)
    summaries = [t["summary"] for t in result["timeline"]]
    assert summaries[0] == "Earlier fact"
    assert summaries[1] == "Later fact"


def test_format_history_empty_facts():
    """format_history with no facts returns an empty timeline."""
    from brain.core.retrieve import format_history
    result = format_history("unknown subject", [])
    assert result["subject"] == "unknown subject"
    assert result["timeline"] == []


# ── _team_visible helper ──────────────────────────────────────────────────────

def test_team_visible_shared_fact_crosses_teams():
    """Facts with visibility='shared' must be visible regardless of team_id."""
    from brain.core.retrieve import _team_visible
    row = {"visibility": "shared", "team_id": "team-a"}
    assert _team_visible(row, "team-b", cross_team=False)


def test_team_visible_private_fact_blocked_for_other_team():
    """Private facts (visibility='team') must not be visible to other teams."""
    from brain.core.retrieve import _team_visible
    row = {"visibility": "team", "team_id": "team-a"}
    assert not _team_visible(row, "team-b", cross_team=False)


def test_team_visible_cross_team_flag_allows_all():
    """cross_team=True must allow all facts regardless of visibility."""
    from brain.core.retrieve import _team_visible
    row = {"visibility": "team", "team_id": "team-a"}
    assert _team_visible(row, "team-b", cross_team=True)


# ── Recency-seeking queries ───────────────────────────────────────────────────

def test_recency_intent_detected():
    """Queries asking for the newest items must be recognised as such."""
    from brain.core.retrieve import _wants_recency
    for q in ("what are the latest emails i received", "recent updates",
              "any new messages today", "show me the newest docs"):
        assert _wants_recency(q), f"should detect recency intent: {q!r}"


def test_topic_queries_are_not_treated_as_recency():
    """A topic question must keep relevance ranking, not be sorted by date."""
    from brain.core.retrieve import _wants_recency
    for q in ("what did we decide about the database",
              "who owns the onboarding flow",
              "which mobile framework did we choose"):
        assert not _wants_recency(q), f"should NOT detect recency intent: {q!r}"


def test_recency_key_orders_newest_first():
    """Sort key must order by event time, falling back when it is absent."""
    import datetime as dt
    from brain.core.retrieve import _recency_key

    newer = {"ep_occurred_at": dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc)}
    older = {"ep_occurred_at": dt.datetime(2026, 8, 29, tzinfo=dt.timezone.utc)}
    # Falls back to introduced_at when the episode has no timestamp
    fallback = {"ep_occurred_at": None, "introduced_at": "2026-09-01T00:00:00Z"}
    undated = {"ep_occurred_at": None, "introduced_at": None}

    ordered = sorted([older, undated, newer, fallback],
                     key=_recency_key, reverse=True)
    assert ordered[0] is newer
    assert ordered[1] is fallback
    assert ordered[2] is older
    assert ordered[3] is undated, "undated facts must sort last, not raise"
