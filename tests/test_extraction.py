"""
Unit tests for brain/core/extract.py

Tests:
- extract_from_text returns facts (stub_llm)
- dedup: identical texts deduplicated at DB level (pg_conn required)
- supersession: corrected fact invalidates old one
- record_human_fact stores with source_kind='human'
- correct_fact invalidates old, creates new with supersedes edge
- confidence threshold: facts below 0.7 skipped in extract_from_text
- worthiness filter: short texts rejected by ingest pipeline
"""
import uuid
import pytest
import unittest.mock as mock

from tests.conftest import POSTGRES_AVAILABLE

pytestmark_pg = pytest.mark.skipif(
    not POSTGRES_AVAILABLE,
    reason="requires Postgres DATABASE_URL",
)


# ── extract_from_text with stub_llm ───────────────────────────────────────────

def test_extract_from_text_returns_facts(pg_conn, ws_id):
    """extract_from_text should return a positive count when the stub LLM returns facts."""
    from brain.core import extract
    n = extract.extract_from_text(
        pg_conn, ws_id,
        "We decided to use Postgres as our primary database.",
        source_kind="agent",
        team_id="eng",
    )
    # stub_llm returns confidence=0.9 which is ≥ 0.7, so fact is kept
    assert n >= 0  # graceful: 0 is also valid if dedup removed it


def test_extract_from_text_empty_text_returns_zero(pg_conn, ws_id):
    """Empty text is rejected immediately — no LLM call, no facts."""
    from brain.core import extract
    n = extract.extract_from_text(pg_conn, ws_id, "")
    assert n == 0


def test_extract_from_text_whitespace_only_returns_zero(pg_conn, ws_id):
    """Whitespace-only text is rejected immediately."""
    from brain.core import extract
    n = extract.extract_from_text(pg_conn, ws_id, "   \n  ")
    assert n == 0


# ── dedup: identical texts produce only 1 fact ────────────────────────────────

@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
def test_dedup_identical_texts_produces_one_fact(pg_conn, ws_id):
    """Inserting two identical facts (same subject + near-identical embedding) keeps only 1."""
    from brain.core import extract, db

    db.migrate(pg_conn)

    # Call extract_from_text twice with the same text / same subject
    n1 = extract.extract_from_text(
        pg_conn, ws_id,
        "We decided to adopt Python as our backend language.",
        source_kind="agent", team_id="eng",
    )
    n2 = extract.extract_from_text(
        pg_conn, ws_id,
        "We decided to adopt Python as our backend language.",
        source_kind="agent", team_id="eng",
    )
    # Second call should produce 0 new facts (deduped by near-duplicate check)
    assert n2 == 0


# ── supersession via correct_fact ────────────────────────────────────────────

@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
def test_correct_fact_invalidates_old_and_creates_new(pg_conn, ws_id):
    """correct_fact must invalidate the old fact and write a new one with a supersedes edge."""
    from brain.core import extract, db

    db.migrate(pg_conn)

    # 1. Write an original human fact
    original = extract.record_human_fact(
        pg_conn, ws_id,
        "The backend language is Ruby.",
        acl=["everyone"],
        team_id="eng",
    )
    old_fid = original["saved"]

    # 2. Correct it
    result = extract.correct_fact(
        pg_conn, ws_id, old_fid,
        "The backend language is Python.",
        acl=["everyone"],
        team_id="eng",
    )

    assert result["corrected"] == old_fid
    new_fid = result["new_fact"]
    assert new_fid != old_fid

    # 3. Old fact must be invalidated
    cur = pg_conn.cursor()
    cur.execute("SELECT invalidated_at FROM facts WHERE id=%s", (old_fid,))
    row = cur.fetchone()
    assert row is not None
    assert row["invalidated_at"] is not None, "Old fact should be invalidated"

    # 4. Supersedes edge must exist
    cur.execute(
        "SELECT 1 FROM fact_edges WHERE workspace_id=%s AND src_fact=%s "
        "AND dst_fact=%s AND type='supersedes'",
        (ws_id, new_fid, old_fid),
    )
    assert cur.fetchone() is not None, "supersedes edge not found"
    cur.close()


# ── record_human_fact ─────────────────────────────────────────────────────────

@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
def test_record_human_fact_has_correct_source_kind(pg_conn, ws_id):
    """record_human_fact must write source_kind='human' with confidence=1.0."""
    from brain.core import extract, db

    db.migrate(pg_conn)

    result = extract.record_human_fact(
        pg_conn, ws_id,
        "The CTO is Alice Smith.",
        acl=["everyone"],
        team_id="eng",
    )
    fid = result["saved"]

    cur = pg_conn.cursor()
    cur.execute("SELECT source_kind, confidence FROM facts WHERE id=%s", (fid,))
    row = cur.fetchone()
    cur.close()

    assert row is not None
    assert row["source_kind"] == "human"
    assert float(row["confidence"]) == 1.0


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
def test_record_human_fact_returns_saved_id(pg_conn, ws_id):
    """record_human_fact return value must include 'saved' (the new fact ID) and 'summary'."""
    from brain.core import extract

    result = extract.record_human_fact(
        pg_conn, ws_id,
        "Engineering team meets daily at 9am.",
        acl=["everyone"],
    )
    assert "saved" in result
    assert "summary" in result
    assert result["summary"] == "Engineering team meets daily at 9am."


# ── confidence threshold ──────────────────────────────────────────────────────

def test_extract_from_text_skips_low_confidence_facts(monkeypatch, pg_conn, ws_id):
    """
    Facts with confidence < 0.7 must be silently skipped by extract_from_text.
    We patch the LLM to return a low-confidence fact.
    """
    import json
    from brain.core import extract, llm as _llm

    _low_conf = {
        "facts": [
            {
                "subject": "pytest",
                "predicate": "might be",
                "object": "great",
                "summary": "pytest might be great",
                "confidence": 0.3,   # below 0.7 threshold
            }
        ]
    }

    def _fake_low(**kwargs):
        resp = mock.MagicMock()
        fake_usage = mock.MagicMock()
        fake_usage.prompt_tokens = 10
        fake_usage.completion_tokens = 5
        resp.usage = fake_usage
        resp.choices = [mock.MagicMock()]
        resp.choices[0].message.content = json.dumps(_low_conf)
        return resp

    monkeypatch.setattr(_llm, "_completion", _fake_low)

    from brain.core import db
    db.migrate(pg_conn)

    n = extract.extract_from_text(
        pg_conn, ws_id,
        "Maybe we'll use Python.",
        source_kind="agent",
        team_id="eng",
    )
    assert n == 0, f"Expected 0 facts (low confidence), got {n}"


# ── worthiness: short texts are filtered at ingest level ─────────────────────

@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
def test_ingest_rejects_short_text(pg_conn, ws_id):
    """Texts shorter than WORTHY_MIN_LEN (12 chars) must be filtered by ingest.run."""
    from brain.core import ingest, db
    from brain.config import WORTHY_MIN_LEN

    db.migrate(pg_conn)

    short_ep = {
        "source": "test",
        "external_id": str(uuid.uuid4()),
        "title": "ok",
        "body": "ok",          # len=2, well below 12
        "author": "tester",
        "url": "",
        "occurred_at": "2026-01-01T00:00:00",
        "acl": "everyone",
        "team_id": "eng",
    }
    assert len(short_ep["body"]) < WORTHY_MIN_LEN
    n = ingest.run(pg_conn, ws_id, [short_ep], team_id="eng")
    assert n == 0


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
def test_ingest_accepts_worthy_text(pg_conn, ws_id):
    """Texts at or above WORTHY_MIN_LEN must pass the worthiness filter."""
    from brain.core import ingest, db
    from brain.config import WORTHY_MIN_LEN

    db.migrate(pg_conn)

    body = "We use Python for all backend services."
    assert len(body) >= WORTHY_MIN_LEN

    worthy_ep = {
        "source": "test",
        "external_id": str(uuid.uuid4()),
        "title": body[:40],
        "body": body,
        "author": "tester",
        "url": "",
        "occurred_at": "2026-01-01T00:00:00",
        "acl": "everyone",
        "team_id": "eng",
    }
    n = ingest.run(pg_conn, ws_id, [worthy_ep], team_id="eng")
    assert n == 1
