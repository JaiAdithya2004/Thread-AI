"""
Regression benchmark test suite — pytest-runnable quality gate.

Runs the full eval harness and asserts every metric stays within the
tolerances defined in evals/thresholds.yaml relative to the baseline
snapshot in evals/baseline.json.

Skipped automatically when:
  - No baseline file exists (evals/baseline.json absent) — run
    `python scripts/bench_snapshot.py` to create one first.
  - The DATABASE_URL env var is absent (no DB connection available).

Mark: @pytest.mark.regression
Run:  pytest tests/test_regression.py -v
"""
import json
import os
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.regression

BASELINE_PATH    = Path("evals/baseline.json")
THRESHOLDS_PATH  = Path("evals/thresholds.yaml")
QUESTIONS_PATH   = Path("evals/questions.yaml")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def thresholds():
    if not THRESHOLDS_PATH.exists():
        pytest.skip(f"No thresholds file at {THRESHOLDS_PATH}")
    with open(THRESHOLDS_PATH) as f:
        return yaml.safe_load(f)["metrics"]


@pytest.fixture(scope="session")
def baseline():
    if not BASELINE_PATH.exists():
        pytest.skip(
            f"No baseline at {BASELINE_PATH}. "
            "Run: python scripts/bench_snapshot.py"
        )
    with open(BASELINE_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def questions():
    with open(QUESTIONS_PATH) as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def eval_results(questions):
    """Run the full eval harness once per session and cache results."""
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set — skipping regression tests")

    from brain.config import WORKSPACE_ID, TEAM_ID, CALLER, CALLER_PRINCIPALS
    from brain.engine import Brain
    from brain.core import db_pg as pg

    ws = os.getenv("BRAIN_WORKSPACE", WORKSPACE_ID)
    brain = Brain(
        workspace_id=ws,
        team_id=TEAM_ID,
        caller=CALLER,
        principals=CALLER_PRINCIPALS,
    )

    conn = pg.connect()
    cur  = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) as n FROM facts WHERE workspace_id=%s AND invalidated_at IS NULL",
        (ws,)
    )
    fact_count = cur.fetchone()["n"]
    cur.close()
    conn.close()

    if fact_count == 0:
        pytest.skip(f"0 active facts in workspace '{ws}' — run seeding first")

    # Import the per-question runner from the eval harness
    from evals.run import _run_question, _aggregate

    K = int(os.getenv("EVAL_K", "5"))
    # Skip LLM-as-judge in CI unless explicitly opted in (saves API quota)
    run_faithfulness = os.getenv("EVAL_FAITHFULNESS", "false").lower() in ("1", "true", "yes")

    results = []
    for q in questions:
        r = _run_question(brain, q, k=K, run_faithfulness=run_faithfulness)
        results.append(r)

    agg = _aggregate(results, k=K)
    return {"results": results, "aggregate": agg, "k": K, "fact_count": fact_count}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _metric_key(metric: str, k: int) -> str:
    """Normalise threshold key (e.g. 'hit_at_k') to actual key ('hit_at_5')."""
    return metric.replace("_at_k", f"_at_{k}")


def _check_metric(metric: str, cfg: dict, current_val, baseline_val, k: int):
    """
    Assert current_val does not represent a regression vs baseline_val.
    Returns (passed: bool, message: str).
    """
    direction = cfg.get("direction", "higher_is_better")
    tolerance = cfg.get("tolerance", 0.10)
    min_value = cfg.get("min_value")

    if current_val is None:
        return True, f"{metric}: not measured (skipped)"

    # Hard floor check
    if min_value is not None and direction == "higher_is_better":
        if current_val < min_value:
            return (False,
                    f"{metric}: {current_val:.3f} < hard floor {min_value:.3f}")

    if baseline_val is None:
        return True, f"{metric}: no baseline value — floor check only"

    delta = current_val - baseline_val
    if direction == "higher_is_better":
        if delta < -tolerance:
            return (False,
                    f"{metric}: {baseline_val:.3f} → {current_val:.3f} "
                    f"(delta={delta:+.3f}, max allowed drop={tolerance:.3f})")
    else:
        if delta > tolerance:
            return (False,
                    f"{metric}: {baseline_val:.3f} → {current_val:.3f} "
                    f"(delta={delta:+.3f}, max allowed increase={tolerance:.3f})")

    return True, f"{metric}: OK ({current_val:.3f})"


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval metric tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRetrievalMetrics:

    def test_hit_at_k(self, eval_results, baseline, thresholds):
        k   = eval_results["k"]
        key = f"hit_at_{k}"
        cur = eval_results["aggregate"].get(key)
        bsl = baseline["aggregate"].get(key)
        passed, msg = _check_metric("hit_at_k", thresholds["hit_at_k"], cur, bsl, k)
        assert passed, msg

    def test_precision_at_k(self, eval_results, baseline, thresholds):
        k   = eval_results["k"]
        key = f"precision_at_{k}"
        cur = eval_results["aggregate"].get(key)
        bsl = baseline["aggregate"].get(key)
        passed, msg = _check_metric("precision_at_k", thresholds["precision_at_k"], cur, bsl, k)
        assert passed, msg

    def test_recall_at_k(self, eval_results, baseline, thresholds):
        k   = eval_results["k"]
        key = f"recall_at_{k}"
        cur = eval_results["aggregate"].get(key)
        bsl = baseline["aggregate"].get(key)
        passed, msg = _check_metric("recall_at_k", thresholds["recall_at_k"], cur, bsl, k)
        assert passed, msg

    def test_mrr(self, eval_results, baseline, thresholds):
        cur = eval_results["aggregate"].get("mrr")
        bsl = baseline["aggregate"].get("mrr")
        passed, msg = _check_metric("mrr", thresholds["mrr"], cur, bsl, eval_results["k"])
        assert passed, msg

    def test_ndcg_at_k(self, eval_results, baseline, thresholds):
        k   = eval_results["k"]
        key = f"ndcg_at_{k}"
        cur = eval_results["aggregate"].get(key)
        bsl = baseline["aggregate"].get(key)
        passed, msg = _check_metric("ndcg_at_k", thresholds["ndcg_at_k"], cur, bsl, k)
        assert passed, msg


# ─────────────────────────────────────────────────────────────────────────────
# Generation quality tests
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerationMetrics:

    def test_answer_correct_rate(self, eval_results, baseline, thresholds):
        cur = eval_results["aggregate"].get("answer_correct_rate")
        bsl = baseline["aggregate"].get("answer_correct_rate")
        passed, msg = _check_metric(
            "answer_correct_rate", thresholds["answer_correct_rate"],
            cur, bsl, eval_results["k"]
        )
        assert passed, msg

    def test_keyword_hit_rate(self, eval_results, baseline, thresholds):
        cur = eval_results["aggregate"].get("keyword_hit_rate")
        bsl = baseline["aggregate"].get("keyword_hit_rate")
        passed, msg = _check_metric(
            "keyword_hit_rate", thresholds["keyword_hit_rate"],
            cur, bsl, eval_results["k"]
        )
        assert passed, msg

    def test_faithfulness_rate(self, eval_results, baseline, thresholds):
        cur = eval_results["aggregate"].get("faithfulness_rate")
        if cur is None:
            pytest.skip("Faithfulness not measured in this run (--no-faithfulness)")
        bsl = baseline["aggregate"].get("faithfulness_rate")
        passed, msg = _check_metric(
            "faithfulness_rate", thresholds["faithfulness_rate"],
            cur, bsl, eval_results["k"]
        )
        assert passed, msg

    def test_abstention_accuracy(self, eval_results, baseline, thresholds):
        cur = eval_results["aggregate"].get("abstention_accuracy")
        bsl = baseline["aggregate"].get("abstention_accuracy")
        passed, msg = _check_metric(
            "abstention_accuracy", thresholds["abstention_accuracy"],
            cur, bsl, eval_results["k"]
        )
        assert passed, msg


# ─────────────────────────────────────────────────────────────────────────────
# Latency tests
# ─────────────────────────────────────────────────────────────────────────────

class TestLatencyMetrics:

    def test_mean_latency(self, eval_results, baseline, thresholds):
        cur = eval_results["aggregate"].get("mean_latency_s")
        bsl = baseline["aggregate"].get("mean_latency_s")
        passed, msg = _check_metric(
            "mean_latency_s", thresholds["mean_latency_s"],
            cur, bsl, eval_results["k"]
        )
        assert passed, msg

    def test_p95_latency(self, eval_results, baseline, thresholds):
        cur = eval_results["aggregate"].get("p95_latency_s")
        bsl = baseline["aggregate"].get("p95_latency_s")
        passed, msg = _check_metric(
            "p95_latency_s", thresholds["p95_latency_s"],
            cur, bsl, eval_results["k"]
        )
        assert passed, msg


# ─────────────────────────────────────────────────────────────────────────────
# Per-question regression gate
# ─────────────────────────────────────────────────────────────────────────────

class TestPerQuestionRegression:
    """Every question that passed in the baseline must still pass."""

    @pytest.fixture(scope="class")
    def passing_in_baseline(self, baseline):
        return {qid for qid, r in baseline.get("per_question", {}).items() if r.get("passed")}

    @pytest.fixture(scope="class")
    def current_by_id(self, eval_results):
        return {r["id"]: r for r in eval_results["results"]}

    def test_no_newly_failing_questions(self, passing_in_baseline, current_by_id):
        """Questions that passed in the baseline must not regress to failing."""
        newly_failing = [
            qid for qid in passing_in_baseline
            if qid in current_by_id and not current_by_id[qid]["passed"]
        ]
        assert not newly_failing, (
            f"Questions newly failing (were passing in baseline): {newly_failing}\n"
            "Fix these regressions or update the baseline with:\n"
            "  python scripts/bench_snapshot.py"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Architectural change guard tests (no DB needed)
# ─────────────────────────────────────────────────────────────────────────────

class TestArchitecturalGuards:
    """
    Lightweight guards that catch the most dangerous config-level regressions
    without needing a DB connection. These always run.
    """

    def test_embed_model_unchanged(self, baseline):
        """Embedding model must match the baseline — changing it silently breaks vector search."""
        from brain.config import EMBED_MODEL
        baseline_meta = baseline.get("meta", {})
        # We record embed model in snapshot going forward; skip if not in older baselines
        baseline_embed = baseline_meta.get("embed_model")
        if baseline_embed is None:
            pytest.skip("Baseline predates embed_model tracking")
        assert EMBED_MODEL == baseline_embed, (
            f"EMBED_MODEL changed: {baseline_embed!r} → {EMBED_MODEL!r}. "
            "Run scripts/reembed_gemini.py + scripts/rebuild_index.py, "
            "then re-snapshot: python scripts/bench_snapshot.py"
        )

    def test_embed_dim_consistent_with_hnsw(self):
        """EMBED_DIM in config must match the vector column dimension in migrations."""
        from brain.config import EMBED_DIM
        migration = Path("migrations/versions/0003_hnsw_768.py")
        if not migration.exists():
            pytest.skip("Migration file not found")
        content = migration.read_text()
        assert f"vector({EMBED_DIM})" in content or f"({EMBED_DIM})" in content, (
            f"EMBED_DIM={EMBED_DIM} not found in HNSW migration — "
            "config and DB schema may be out of sync"
        )

    def test_dedup_threshold_in_range(self):
        """DEDUP_THRESHOLD must be in [0.80, 0.99] — outside this range is dangerous."""
        from brain.config import DEDUP_THRESHOLD
        assert 0.80 <= DEDUP_THRESHOLD <= 0.99, (
            f"DEDUP_THRESHOLD={DEDUP_THRESHOLD} is outside the safe range [0.80, 0.99]. "
            "Run the dedup calibration tests before changing this value."
        )

    def test_confidence_threshold_in_range(self):
        """CONFIDENCE_THRESHOLD controls abstention — must be in [0.20, 0.60]."""
        from brain.config import CONFIDENCE_THRESHOLD
        assert 0.20 <= CONFIDENCE_THRESHOLD <= 0.60, (
            f"CONFIDENCE_THRESHOLD={CONFIDENCE_THRESHOLD} is outside the safe range. "
            "Run abstention accuracy eval before changing this value."
        )

    def test_vec_column_name_in_search(self):
        """search_vec() must query fact_vec_768, not the legacy 'embedding' column."""
        import inspect
        from brain.core import db_pg
        src = inspect.getsource(db_pg.search_vec)
        assert "fact_vec_768" in src, (
            "search_vec() does not reference 'fact_vec_768'. "
            "Vector search is broken — check db_pg.py."
        )
        assert "embedding IS NOT NULL" not in src and \
               "embedding <=" not in src, (
            "search_vec() still references the legacy 'embedding' column. "
            "This silently disables vector search."
        )

    def test_dedup_column_name_in_extract(self):
        """_find_near_duplicate() must query fact_vec_768, not the legacy 'embedding' column."""
        import inspect
        from brain.core import extract
        src = inspect.getsource(extract._find_near_duplicate)
        assert "fact_vec_768" in src, (
            "_find_near_duplicate() does not reference 'fact_vec_768'. "
            "Deduplication is silently disabled."
        )

    def test_injection_patterns_defined(self):
        """_INJECTION_PATTERNS must exist and contain at least 5 patterns."""
        from brain.core.ingest import _INJECTION_PATTERNS
        assert len(_INJECTION_PATTERNS) >= 5, (
            f"Only {len(_INJECTION_PATTERNS)} injection patterns defined. "
            "Expected at least 5 — check ingest.py."
        )

    def test_format_answer_returns_conflicts_key(self):
        """format_answer() must always return a 'conflicts' key."""
        from brain.core import retrieve
        result = retrieve.format_answer("test query", [])
        assert "conflicts" in result, (
            "format_answer() no longer returns 'conflicts' key — "
            "conflict surfacing has been removed or broken."
        )

    def test_mcp_correct_fact_checks_ownership(self):
        """correct_fact MCP tool must contain ownership check logic."""
        import inspect
        from brain.wrappers import mcp_server
        src = inspect.getsource(mcp_server.correct_fact)
        assert "not authorized" in src or "caller_allowed" in src, (
            "correct_fact() no longer contains ownership check. "
            "Any MCP caller can now invalidate any fact — security regression."
        )
