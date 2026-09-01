"""
Negative-control tests for the retrieval benchmark.

These exist because the PREVIOUS harness could not detect a retrieval miss:
it built ground truth by filtering the facts retrieval had already returned,
so a fact that was never retrieved was absent from both the numerator and the
denominator of recall. Recall could not fall. The benchmark reported numbers
that were structurally incapable of failing.

A metric that cannot go down is not measuring anything. These tests prove the
new metrics respond to a fact being withheld.

No database rows are modified: exclusion is applied to the retrieved list,
never to the corpus.
"""
import math

import pytest

from evals.benchmark import hit_at_k, precision_at_k, recall_at_k, mrr, ndcg_at_k


# ── The control: withhold a known-relevant fact ───────────────────────────────

RETRIEVED = ["fact_A", "fact_B", "fact_C", "fact_D", "fact_E"]
RELEVANT_SINGLE = ["fact_A"]
RELEVANT_PAIR   = ["fact_A", "fact_C"]
K = 5


def _without(fid):
    """Retrieval output with one fact withheld — the corpus is untouched."""
    return [f for f in RETRIEVED if f != fid]


def test_recall_falls_when_the_only_relevant_fact_is_withheld():
    before = recall_at_k(RETRIEVED, RELEVANT_SINGLE, K)
    after  = recall_at_k(_without("fact_A"), RELEVANT_SINGLE, K)
    assert before == 1.0
    assert after == 0.0
    assert after < before, "recall must fall when a relevant fact is not retrieved"


def test_hit_falls_when_the_only_relevant_fact_is_withheld():
    assert hit_at_k(RETRIEVED, RELEVANT_SINGLE, K) == 1.0
    assert hit_at_k(_without("fact_A"), RELEVANT_SINGLE, K) == 0.0


def test_recall_is_partial_when_one_of_two_relevant_facts_is_withheld():
    """The case that matters most: hit@k still passes, recall must still drop."""
    before = recall_at_k(RETRIEVED, RELEVANT_PAIR, K)
    after  = recall_at_k(_without("fact_C"), RELEVANT_PAIR, K)
    assert before == 1.0
    assert after == 0.5
    # hit@k is insensitive here — which is exactly why recall must not be
    assert hit_at_k(_without("fact_C"), RELEVANT_PAIR, K) == 1.0


def test_mrr_reflects_rank_degradation():
    assert mrr(RETRIEVED, RELEVANT_SINGLE) == 1.0        # rank 1
    assert mrr(_without("fact_A"), RELEVANT_SINGLE) == 0.0
    # relevant fact demoted from rank 1 to rank 3
    assert mrr(["x", "y", "fact_A"], RELEVANT_SINGLE) == pytest.approx(1 / 3)


def test_ndcg_reflects_rank_degradation():
    top    = ndcg_at_k(["fact_A", "x", "y"], RELEVANT_SINGLE, K)
    demoted = ndcg_at_k(["x", "y", "fact_A"], RELEVANT_SINGLE, K)
    assert top == 1.0
    assert 0.0 < demoted < top
    assert ndcg_at_k(_without("fact_A"), RELEVANT_SINGLE, K) == 0.0


def test_precision_falls_when_relevant_fact_withheld():
    assert precision_at_k(RETRIEVED, RELEVANT_SINGLE, K) == 0.2
    assert precision_at_k(_without("fact_A"), RELEVANT_SINGLE, K) == 0.0


# ── Ground truth must not come from retrieval output ──────────────────────────

def test_ground_truth_is_independent_of_retrieval():
    """A relevant fact absent from retrieval must still count in the denominator.

    This is the precise defect in the old harness. If relevant_ids were derived
    from retrieved_ids, recall here would be 1.0 (1 of 1 'found') instead of 0.5.
    """
    relevant = ["fact_A", "never_retrieved"]
    assert recall_at_k(RETRIEVED, relevant, K) == 0.5


def test_unanswerable_questions_are_not_scored_as_failures():
    """No relevant facts => None (not applicable), never 0.0 (a failure)."""
    assert recall_at_k(RETRIEVED, [], K) is None
    assert ndcg_at_k(RETRIEVED, [], K) is None


# ── K consistency ─────────────────────────────────────────────────────────────

def test_k_boundary_is_enforced():
    """A relevant fact beyond K must not count — the 8-vs-5 mismatch bug."""
    eight = ["a", "b", "c", "d", "e", "fact_A", "g", "h"]
    assert hit_at_k(eight, RELEVANT_SINGLE, 5) == 0.0
    assert hit_at_k(eight, RELEVANT_SINGLE, 8) == 1.0
    assert recall_at_k(eight, RELEVANT_SINGLE, 5) == 0.0
    assert recall_at_k(eight, RELEVANT_SINGLE, 8) == 1.0


# ── Ground-truth file integrity ───────────────────────────────────────────────

def test_ground_truth_file_resolves_and_pins_workspace():
    from evals.benchmark import load_ground_truth
    doc, gt = load_ground_truth("evals/ground_truth.yaml")
    assert doc["workspace"] == "ws_local", "benchmark workspace must be pinned"
    answerable = [q for q in gt.values() if q["answerable"]]
    assert answerable, "expected at least one answerable question"
    for qid, spec in gt.items():
        if spec["answerable"]:
            assert spec["relevant"], f"{qid}: answerable but has no relevant ids"
        else:
            assert spec["relevant"] == [], f"{qid}: unanswerable must have no ids"
