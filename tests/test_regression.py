"""
Regression gate for Thread's quality.

Runs the retrieval benchmark (evals/bench.py, Part A) against a local database
and checks it against the floors in evals/thresholds.yaml and the recorded
baseline in evals/baseline.json. The retrieval check needs a database and an
embedding key but no chat model, so it is cheap enough for every CI run.

The LLM-judged parts (answer quality, supersession, end to end) cost tokens and
are not deterministic, so they run only when BENCH_LLM=1 and are compared with
the same floors. Refresh the baseline after an intentional change with:

    python -m evals.bench --out evals/results/bench_run_1.json   # repeat for 2, 3
    python -m evals.aggregate --write-baseline

Skipped automatically when DATABASE_URL is unset or points at a non-local host
(the benchmark deletes and recreates its ws_bench_* workspaces).

Mark: @pytest.mark.regression
Run:  pytest tests/test_regression.py -v
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

pytestmark = pytest.mark.regression

EVALS = Path(__file__).resolve().parent.parent / "evals"
BASELINE_PATH = EVALS / "baseline.json"
THRESHOLDS_PATH = EVALS / "thresholds.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def thresholds():
    return yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def baseline():
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _local_db() -> bool:
    url = os.getenv("DATABASE_URL", "")
    return bool(url) and (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


@pytest.fixture(scope="session")
def bench_results():
    """Run the benchmark once per session."""
    if not _local_db():
        pytest.skip("needs a local DATABASE_URL (the benchmark resets ws_bench_* workspaces)")
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("needs GEMINI_API_KEY for query embeddings")
    import yaml as _yaml
    from evals import bench
    from brain.core import db

    corpus = _yaml.safe_load(bench.CORPUS_PATH.read_text(encoding="utf-8"))
    conn = db.connect()
    db.migrate(conn)
    llm = os.getenv("BENCH_LLM") == "1"
    # A provider outage or an exhausted quota is not a quality regression. Scoring
    # zero vectors would report one, so skip with the reason instead.
    try:
        with bench.embedding_guard():
            res = {"A": bench.part_a(corpus, conn, answers=llm)}
            if llm:
                if not (res["A"].get("answers") or {}).get("answer_correct", {}).get("n"):
                    pytest.skip("no answers were synthesized (model provider unavailable or out of credit)")
                res["B"] = bench.part_b(corpus, conn)
                res["C"] = bench.part_c(corpus, conn)
    except RuntimeError as e:
        pytest.skip(f"benchmark infrastructure unavailable: {e}")
    return res


def _value(res: dict, key: str):
    """Resolve a flat key such as 'retrieval.production.hit@5' in bench output."""
    parts = key.split(".")
    if parts[0] == "retrieval":
        v = res["A"]["retrieval"][parts[1]][parts[2]]
    elif parts[0] == "answers":
        v = res["A"]["answers"][parts[1]]
    elif parts[0] == "supersession":
        v = res["B"]["methods"][parts[1]][parts[2]]
    elif parts[0] == "end_to_end":
        v = res["C"][parts[1]][parts[2]]
    else:
        raise KeyError(key)
    return v["value"] if isinstance(v, dict) else v


def _check(key: str, cfg: dict, current, baseline_val):
    higher = cfg.get("direction", "higher_is_better") == "higher_is_better"
    floor, tol = cfg.get("floor"), cfg.get("tolerance", 0.05)
    if current is None:
        pytest.fail(f"{key}: not measured")
    if floor is not None:
        assert (current >= floor) if higher else (current <= floor), (
            f"{key}: {current:.3f} breaches the hard limit {floor:.3f}")
    if baseline_val is not None:
        delta = current - baseline_val
        assert (delta >= -tol) if higher else (delta <= tol), (
            f"{key}: {baseline_val:.3f} -> {current:.3f} (allowed change {tol:.3f}). "
            "If intentional, refresh the baseline (see this file's docstring).")


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval gate (every run)
# ─────────────────────────────────────────────────────────────────────────────

class TestRetrievalGate:

    @pytest.mark.parametrize("key", [
        "retrieval.production.hit@5", "retrieval.production.recall@5",
        "retrieval.production.mrr", "retrieval.production.ndcg@5",
    ])
    def test_metric(self, key, bench_results, baseline, thresholds):
        cfg = thresholds["retrieval"][key]
        _check(key, cfg, _value(bench_results, key), baseline["aggregate"].get(key))

    def test_hybrid_not_worse_than_full_text(self, bench_results):
        """Semantic search carries the system; if it ever falls below keyword search, it is broken."""
        r = bench_results["A"]["retrieval"]
        assert r["vector"]["hit@5"]["value"] > r["fulltext"]["hit@5"]["value"]


# ─────────────────────────────────────────────────────────────────────────────
# LLM-judged gate (BENCH_LLM=1)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(os.getenv("BENCH_LLM") != "1", reason="set BENCH_LLM=1 to run the LLM-judged benchmark")
class TestLlmGate:

    def test_metrics(self, bench_results, baseline, thresholds):
        failures = []
        for key, cfg in thresholds["llm"].items():
            try:
                _check(key, cfg, _value(bench_results, key), baseline["aggregate"].get(key))
            except AssertionError as e:
                failures.append(str(e))
        assert not failures, "\n".join(failures)


# ─────────────────────────────────────────────────────────────────────────────
# Architecture guards (no database needed)
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
        """EMBED_DIM in config must match the vector column dimension in migrations.

        The dimension is declared where the column is created (0002), not in the
        HNSW migration (0003) which only builds an index over an existing column.
        """
        from brain.config import EMBED_DIM
        migration = Path("migrations/versions/0002_embed_768.py")
        if not migration.exists():
            pytest.skip("Migration file not found")
        content = migration.read_text()
        assert f"vector({EMBED_DIM})" in content, (
            f"EMBED_DIM={EMBED_DIM} not declared in the vector column migration — "
            "config and DB schema may be out of sync"
        )

        # The index migration must target the column for that same dimension.
        index_migration = Path("migrations/versions/0003_hnsw_768.py")
        if index_migration.exists():
            assert f"fact_vec_{EMBED_DIM}" in index_migration.read_text(), (
                f"HNSW migration does not index fact_vec_{EMBED_DIM} — "
                "the index and the configured embedding dimension disagree"
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
        """correct_fact MCP tool must contain ownership check logic.

        Read the source from disk rather than importing the module: importing
        it constructs a module-level Brain(), which opens a database connection
        at import time. This guard only inspects text, so it must not require a
        live database to run.
        """
        source = Path("brain/wrappers/mcp_server.py").read_text(encoding="utf-8")
        start = source.find("def correct_fact")
        assert start != -1, "correct_fact() not found in mcp_server.py"
        # Bound the slice at the next top-level def so the assertion cannot be
        # satisfied by ownership logic belonging to some other tool.
        end = source.find("\ndef ", start + 1)
        src = source[start:end if end != -1 else len(source)]
        assert "not authorized" in src or "caller_allowed" in src, (
            "correct_fact() no longer contains ownership check. "
            "Any MCP caller can now invalidate any fact — security regression."
        )
