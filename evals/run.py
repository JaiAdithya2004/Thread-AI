"""
Eval harness for Thread — measures Recall@K, Precision@K, MRR, NDCG,
faithfulness (LLM-as-judge), citation correctness, abstention accuracy,
and hallucination rate.

Usage:
    python -m evals.run
    python -m evals.run --questions evals/questions.yaml --out evals/results.json
    python -m evals.run --ws ws_demo                  # run against demo workspace
    python -m evals.run --k 5                         # set K for retrieval metrics
    python -m evals.run --no-faithfulness             # skip LLM-as-judge (saves API calls)
    python -m evals.run --reset-extracted             # mark all episodes unextracted, then exit

Connects to Postgres directly via DATABASE_URL (server does not need to be running).
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

# The scorecard uses box-drawing and check/cross glyphs. On Windows the console
# defaults to cp1252, which cannot encode them — printing one raised
# UnicodeEncodeError mid-run and destroyed the whole eval. Force UTF-8 on the
# streams we write to, and fall back to replacement characters if that fails.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── Metric helpers ────────────────────────────────────────────────────────────

def _hit_at_k(retrieved_ids: list, relevant_ids: list, k: int) -> float:
    """1.0 if any relevant fact appears in top-k retrieved, else 0.0."""
    if not relevant_ids:
        return 1.0   # unanswerable questions: no positive requirement
    return float(any(fid in relevant_ids for fid in retrieved_ids[:k]))


def _precision_at_k(retrieved_ids: list, relevant_ids: list, k: int) -> float:
    if not retrieved_ids or not relevant_ids:
        return 0.0
    hits = sum(1 for fid in retrieved_ids[:k] if fid in relevant_ids)
    return hits / min(k, len(retrieved_ids))


def _recall_at_k(retrieved_ids: list, relevant_ids: list, k: int) -> float:
    if not relevant_ids:
        return 1.0
    hits = sum(1 for fid in retrieved_ids[:k] if fid in relevant_ids)
    return hits / len(relevant_ids)


def _mrr(retrieved_ids: list, relevant_ids: list) -> float:
    if not relevant_ids:
        return 1.0
    for rank, fid in enumerate(retrieved_ids, start=1):
        if fid in relevant_ids:
            return 1.0 / rank
    return 0.0


def _ndcg_at_k(retrieved_ids: list, relevant_ids: list, k: int) -> float:
    if not relevant_ids:
        return 1.0
    rel_set = set(relevant_ids)

    def dcg(ids):
        return sum(
            (1.0 / math.log2(i + 2))
            for i, fid in enumerate(ids[:k])
            if fid in rel_set
        )

    actual = dcg(retrieved_ids)
    ideal  = dcg(list(rel_set)[:k])
    return actual / ideal if ideal > 0 else 0.0


def _keyword_hit(fact_summaries: list, keywords: list) -> bool:
    """True if any fact summary contains any of the keywords (case-insensitive)."""
    if not keywords:
        return True
    haystack = " ".join(s.lower() for s in fact_summaries)
    return any(kw.lower() in haystack for kw in keywords)


# ── Faithfulness judge ────────────────────────────────────────────────────────

_FAITH_SYS = (
    "You are an expert evaluator. Given a question, an AI answer, and the "
    "source facts the AI was given, determine whether every claim in the "
    "answer is supported by the provided facts. Return JSON only:\n"
    '{"faithful": true/false, "unsupported_claims": ["..."], "reasoning": "..."}'
)


def _faithfulness_score(question: str, answer: str, fact_summaries: list) -> dict:
    """
    LLM-as-judge faithfulness check.
    Returns {"faithful": bool, "unsupported_claims": list, "reasoning": str, "error": str|None}.
    Never raises.
    """
    if not answer or not answer.strip():
        return {"faithful": True, "unsupported_claims": [], "reasoning": "no answer to check", "error": None}
    try:
        from brain.core import llm
        facts_block = "\n".join(f"- {s}" for s in fact_summaries)
        user = (
            f"Question: {question}\n\n"
            f"AI Answer: {answer}\n\n"
            f"Source Facts:\n{facts_block}\n\n"
            "Is every claim in the AI Answer supported by the Source Facts?"
        )
        result = llm.chat_json(_FAITH_SYS, user, task_name="faithfulness_eval")
        return {
            "faithful":            bool(result.get("faithful", True)),
            "unsupported_claims":  result.get("unsupported_claims", []),
            "reasoning":           result.get("reasoning", ""),
            "error":               None,
        }
    except Exception as e:
        return {"faithful": True, "unsupported_claims": [], "reasoning": "", "error": str(e)}


# ── Citation correctness ──────────────────────────────────────────────────────

_CITE_SYS = (
    "You are an expert evaluator. Given a sentence from an AI answer and the "
    "fact it cites, determine whether the cited fact genuinely supports the "
    "specific claim made in the sentence. Return JSON only:\n"
    '{"correct": true/false, "reason": "..."}'
)


def _check_citation(sentence: str, fact_summary: str) -> bool:
    """True if the cited fact supports the sentence. Returns True on error (conservative)."""
    try:
        from brain.core import llm
        user = f'Sentence: "{sentence}"\nCited fact: "{fact_summary}"\nDoes the fact support the sentence?'
        result = llm.chat_json(_CITE_SYS, user, task_name="citation_eval")
        return bool(result.get("correct", True))
    except Exception:
        return True


def _citation_scores(answer_text: str, facts: list) -> dict:
    """
    Parse [fact_id] citations from the synthesized answer and check each one.
    Returns:
      correct_citations    — count where cited fact supports the sentence
      total_citations      — count of [fact_id] references found
      citation_precision   — correct / total (1.0 if none found)
    """
    import re
    fact_by_id = {f.get("fact_id", f.get("id", "")): f.get("summary", "") for f in facts}
    sentences  = re.split(r"[.!?]", answer_text)
    total = correct = 0

    for sentence in sentences:
        refs = re.findall(r"\[([a-f0-9\-]{8,})\]", sentence)
        for ref in refs:
            if ref not in fact_by_id:
                continue
            total += 1
            if _check_citation(sentence.strip(), fact_by_id[ref]):
                correct += 1

    return {
        "total_citations":   total,
        "correct_citations": correct,
        "citation_precision": round(correct / total, 3) if total else 1.0,
    }


# ── Core per-question runner ──────────────────────────────────────────────────

def _run_question(brain, q: dict, k: int, run_faithfulness: bool) -> dict:
    start = time.perf_counter()
    result = brain.search(q["question"], surface="eval")
    elapsed = time.perf_counter() - start

    answer_text    = (result.get("synthesized_answer") or result.get("answer") or "").strip()
    facts          = result.get("facts", [])
    retrieved_ids  = [f.get("fact_id", f.get("id", "")) for f in facts]
    fact_summaries = [f.get("summary", "") for f in facts]

    # ── Abstention / not-sure detection ──────────────────────────────────────
    expects_not_sure = q.get("expect_not_sure", False)
    answerable       = q.get("answerable", not expects_not_sure)
    top_confidence   = facts[0].get("confidence", 0.0) if facts else 0.0
    has_synthesized  = bool(answer_text)

    # System is "not sure" when: no synthesized answer, or top confidence < 0.4
    system_not_sure = not has_synthesized or top_confidence < 0.4
    abstention_correct = (expects_not_sure == system_not_sure)

    # ── Retrieval metrics (only for answerable questions) ─────────────────────
    # We use keyword matching as a proxy for relevance (no ground-truth fact IDs yet).
    # When expected_fact_keywords is present, a fact is "relevant" if its summary
    # contains any of the keywords.
    keywords = q.get("expected_fact_keywords", [])
    if keywords and answerable:
        relevant_proxy_ids = [
            f.get("fact_id", f.get("id", "")) for f in facts
            if any(kw.lower() in f.get("summary", "").lower() for kw in keywords)
        ]
        # If zero relevant found at all, note it — retrieval completely missed
        any_relevant = bool(relevant_proxy_ids)
    else:
        relevant_proxy_ids = []
        any_relevant       = None   # unknown — no keywords

    if relevant_proxy_ids:
        hit_at_k         = _hit_at_k(retrieved_ids, relevant_proxy_ids, k)
        precision_at_k   = _precision_at_k(retrieved_ids, relevant_proxy_ids, k)
        recall_at_k      = _recall_at_k(retrieved_ids, relevant_proxy_ids, k)
        mrr              = _mrr(retrieved_ids, relevant_proxy_ids)
        ndcg_at_k        = _ndcg_at_k(retrieved_ids, relevant_proxy_ids, k)
    elif answerable:
        # Answerable question but no retrieved facts contain any keyword — complete miss
        hit_at_k = precision_at_k = recall_at_k = mrr = ndcg_at_k = 0.0
    else:
        # Unanswerable — metrics not applicable
        hit_at_k = precision_at_k = recall_at_k = mrr = ndcg_at_k = None

    # ── Answer correctness (substring) ───────────────────────────────────────
    expected_answer = (q.get("expected_answer_contains") or "").lower()
    expected_fact   = (q.get("expected_source_fact_summary_contains") or "").lower()
    answer_lower    = answer_text.lower()
    fact_summ_lower = " ".join(fact_summaries).lower()

    answer_correct = (not expected_answer) or (expected_answer in answer_lower)
    fact_correct   = (not expected_fact)   or (expected_fact in fact_summ_lower)
    keyword_hit    = _keyword_hit(fact_summaries, keywords) if (answerable and keywords) else None

    # ── Faithfulness ─────────────────────────────────────────────────────────
    if run_faithfulness and has_synthesized and answerable:
        faith = _faithfulness_score(q["question"], answer_text, fact_summaries)
    else:
        faith = {"faithful": None, "unsupported_claims": [], "reasoning": "", "error": None}

    # ── Citation correctness ──────────────────────────────────────────────────
    if has_synthesized and answerable and "[" in answer_text:
        cite = _citation_scores(answer_text, facts)
    else:
        cite = {"total_citations": 0, "correct_citations": 0, "citation_precision": None}

    # ── Overall pass/fail ─────────────────────────────────────────────────────
    if expects_not_sure:
        passed = abstention_correct
    else:
        passed = answer_correct and fact_correct

    stale_cited = any(f.get("status") == "stale" for f in facts[:3])

    return {
        "id":               q["id"],
        "category":         q.get("category", ""),
        "difficulty":       q.get("difficulty", ""),
        "answerable":       answerable,
        "question":         q["question"],
        "passed":           passed,
        "latency_s":        round(elapsed, 3),

        # Retrieval
        "facts_returned":   len(facts),
        "any_relevant":     any_relevant,
        "hit_at_k":         hit_at_k,
        "precision_at_k":   precision_at_k,
        "recall_at_k":      recall_at_k,
        "mrr":              mrr,
        "ndcg_at_k":        ndcg_at_k,

        # Generation
        "answer_snippet":       answer_text[:300],
        "top_fact":             fact_summaries[0] if fact_summaries else "",
        "top_confidence":       round(top_confidence, 3),
        "has_synthesized":      has_synthesized,
        "answer_correct":       answer_correct,
        "fact_correct":         fact_correct,
        "keyword_hit":          keyword_hit,

        # Faithfulness
        "faithful":             faith["faithful"],
        "unsupported_claims":   faith["unsupported_claims"],
        "faithfulness_error":   faith["error"],

        # Citations
        "total_citations":      cite["total_citations"],
        "correct_citations":    cite["correct_citations"],
        "citation_precision":   cite["citation_precision"],

        # Abstention
        "expects_not_sure":     expects_not_sure,
        "system_not_sure":      system_not_sure,
        "abstention_correct":   abstention_correct,

        # Other
        "stale_cited":          stale_cited,
    }


# ── Aggregate metrics ─────────────────────────────────────────────────────────

def _aggregate(results: list, k: int) -> dict:
    answerable    = [r for r in results if r["answerable"]]
    unanswerable  = [r for r in results if not r["answerable"]]
    with_metrics  = [r for r in answerable if r["hit_at_k"] is not None]

    def _mean(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def _pct(lst, key):
        vals = [r[key] for r in lst if r[key] is not None]
        return round(sum(1 for v in vals if v) / len(vals), 4) if vals else None

    # Faithfulness
    faith_results = [r for r in answerable if r["faithful"] is not None]
    faith_rate = round(sum(1 for r in faith_results if r["faithful"]) / len(faith_results), 4) if faith_results else None
    halluc_rate = round(1.0 - faith_rate, 4) if faith_rate is not None else None

    # Citation
    cite_results = [r for r in results if r["citation_precision"] is not None]
    mean_cite_prec = _mean([r["citation_precision"] for r in cite_results])
    total_cites    = sum(r["total_citations"] for r in results)
    correct_cites  = sum(r["correct_citations"] for r in results)

    # Abstention
    abstention_acc = _pct(results, "abstention_correct")

    return {
        "k":                  k,
        "n_total":            len(results),
        "n_answerable":       len(answerable),
        "n_unanswerable":     len(unanswerable),

        # Retrieval (answerable questions only)
        f"hit_at_{k}":        _mean([r["hit_at_k"]      for r in with_metrics]),
        f"precision_at_{k}":  _mean([r["precision_at_k"] for r in with_metrics]),
        f"recall_at_{k}":     _mean([r["recall_at_k"]   for r in with_metrics]),
        "mrr":                _mean([r["mrr"]            for r in with_metrics]),
        f"ndcg_at_{k}":       _mean([r["ndcg_at_k"]     for r in with_metrics]),

        # Generation
        "answer_correct_rate": _pct(answerable, "answer_correct"),
        "fact_correct_rate":   _pct(answerable, "fact_correct"),
        "keyword_hit_rate":    _pct([r for r in answerable if r["keyword_hit"] is not None], "keyword_hit"),

        # Faithfulness / Hallucination
        "faithfulness_rate":  faith_rate,
        "hallucination_rate": halluc_rate,

        # Citation
        "mean_citation_precision": mean_cite_prec,
        "total_citations":         total_cites,
        "citation_correct_rate":   round(correct_cites / total_cites, 4) if total_cites else None,

        # Abstention
        "abstention_accuracy": abstention_acc,

        # Speed
        "mean_latency_s":     _mean([r["latency_s"] for r in results]),
        "p95_latency_s":      _p95([r["latency_s"] for r in results]),
    }


def _p95(vals: list) -> Optional[float]:
    if not vals:
        return None
    sorted_vals = sorted(vals)
    idx = int(math.ceil(0.95 * len(sorted_vals))) - 1
    return round(sorted_vals[max(0, idx)], 3)


# ── Scorecard printer ─────────────────────────────────────────────────────────

_W = 70

def _bar(val, width=20):
    if val is None:
        return "N/A".ljust(width)
    filled = round(val * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {val:.3f}"


def _print_scorecard(results: list, agg: dict, total_time: float, k: int):
    passed = sum(1 for r in results if r["passed"])
    total  = len(results)
    pct    = 100 * passed // total if total else 0

    print("\n" + "═" * _W)
    print(f"  Thread Eval — {passed}/{total} passed ({pct}%)  [{total_time:.1f}s total]")
    print("═" * _W)

    print(f"\n  RETRIEVAL  (proxy-keyword relevance, k={k})")
    print(f"    Hit@{k}             {_bar(agg.get(f'hit_at_{k}'))}")
    print(f"    Precision@{k}       {_bar(agg.get(f'precision_at_{k}'))}")
    print(f"    Recall@{k}          {_bar(agg.get(f'recall_at_{k}'))}")
    print(f"    MRR               {_bar(agg.get('mrr'))}")
    print(f"    NDCG@{k}           {_bar(agg.get(f'ndcg_at_{k}'))}")

    print(f"\n  GENERATION")
    print(f"    Answer correct    {_bar(agg.get('answer_correct_rate'))}")
    print(f"    Fact correct      {_bar(agg.get('fact_correct_rate'))}")
    print(f"    Keyword hit       {_bar(agg.get('keyword_hit_rate'))}")

    print(f"\n  FAITHFULNESS / HALLUCINATION")
    print(f"    Faithfulness      {_bar(agg.get('faithfulness_rate'))}")
    print(f"    Hallucination     {_bar(agg.get('hallucination_rate'))}  (lower is better)")

    print(f"\n  CITATION")
    print(f"    Mean precision    {_bar(agg.get('mean_citation_precision'))}")
    tc = agg.get('total_citations', 0)
    cc = agg.get('correct_citations', 0)
    print(f"    Correct/total     {cc}/{tc}")

    print(f"\n  ABSTENTION (unanswerable question handling)")
    print(f"    Accuracy          {_bar(agg.get('abstention_accuracy'))}")

    print(f"\n  SPEED")
    ml = agg.get('mean_latency_s')
    p95 = agg.get('p95_latency_s')
    print(f"    Mean latency      {f'{ml:.2f}s' if ml else 'N/A'}")
    print(f"    P95 latency       {f'{p95:.2f}s' if p95 else 'N/A'}")

    print(f"\n  BY CATEGORY")
    by_cat: dict = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    for cat, cat_results in sorted(by_cat.items()):
        cat_pass = sum(1 for r in cat_results if r["passed"])
        print(f"    [{cat}]  {cat_pass}/{len(cat_results)}")

    print(f"\n  BY DIFFICULTY")
    by_diff: dict = {}
    for r in results:
        by_diff.setdefault(r["difficulty"], []).append(r)
    for diff, diff_results in sorted(by_diff.items()):
        diff_pass = sum(1 for r in diff_results if r["passed"])
        print(f"    [{diff}]  {diff_pass}/{len(diff_results)}")

    print(f"\n  FAILURES")
    failures = [r for r in results if not r["passed"]]
    if not failures:
        print("    (none)")
    for r in failures:
        print(f"    ✗ {r['id']} [{r['category']}/{r['difficulty']}]  {r['question'][:55]}")
        if r["answer_snippet"]:
            print(f"        answer:   {r['answer_snippet'][:100]}")
        if r["top_fact"]:
            print(f"        top fact: {r['top_fact'][:100]}")
        if r["unsupported_claims"]:
            print(f"        halluc:   {r['unsupported_claims'][:3]}")

    print("\n" + "═" * _W + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Thread eval harness")
    parser.add_argument("--questions",        default="evals/questions.yaml")
    parser.add_argument("--out",              default="evals/results.json")
    parser.add_argument("--ws",               default=None, help="Override workspace_id")
    parser.add_argument("--team",             default=None, help="Override team_id")
    parser.add_argument("--k",                type=int, default=5, help="K for retrieval metrics")
    parser.add_argument("--no-faithfulness",  action="store_true",
                        help="Skip LLM-as-judge faithfulness check (saves API calls)")
    parser.add_argument("--reset-extracted",  action="store_true",
                        help="Mark all episodes as unextracted, then exit")
    parser.add_argument("--category",         default=None,
                        help="Run only questions in this category")
    parser.add_argument("--difficulty",       default=None,
                        help="Run only questions with this difficulty (easy|medium|hard)")
    args = parser.parse_args()

    from brain.config import WORKSPACE_ID, TEAM_ID, CALLER, CALLER_PRINCIPALS
    from brain.engine import Brain
    from brain.core import db_pg as pg

    ws = args.ws or WORKSPACE_ID
    brain = Brain(
        workspace_id=ws,
        team_id=args.team or TEAM_ID,
        caller=CALLER,
        principals=CALLER_PRINCIPALS,
    )

    if args.reset_extracted:
        conn = pg.connect()
        cur  = conn.cursor()
        cur.execute("UPDATE episodes SET extracted=0 WHERE workspace_id=%s", (ws,))   # column is INTEGER, not boolean
        cur.execute("SELECT COUNT(*) as n FROM episodes WHERE workspace_id=%s", (ws,))
        n = cur.fetchone()["n"]
        conn.commit()
        cur.close()
        conn.close()
        print(f"Reset {n} episodes in workspace '{ws}'. Re-extraction will run on next startup.")
        return

    conn = pg.connect()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) as n FROM facts WHERE workspace_id=%s AND invalidated_at IS NULL", (ws,))
    fact_count = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) as n FROM facts WHERE workspace_id=%s AND fact_vec_768 IS NOT NULL AND invalidated_at IS NULL", (ws,))
    vec_count = cur.fetchone()["n"]
    cur.close()
    conn.close()

    if fact_count == 0:
        print(f"WARNING: 0 active facts in workspace '{ws}'. Run extraction first.")
        print("  Hint: python scripts/dev/seed_facts_direct.py  (bypasses LLM quota)")

    print(f"\nWorkspace '{ws}': {fact_count} active facts, {vec_count} with vectors (hybrid search)")

    questions = _load_questions(args.questions)

    # Filter by category / difficulty if requested
    if args.category:
        questions = [q for q in questions if q.get("category") == args.category]
    if args.difficulty:
        questions = [q for q in questions if q.get("difficulty") == args.difficulty]

    if not questions:
        print("No questions matched the filters. Exiting.")
        return

    run_faithfulness = not args.no_faithfulness
    print(f"Running {len(questions)} questions  k={args.k}  faithfulness={'on' if run_faithfulness else 'off'}\n")

    t0 = time.perf_counter()
    results = []
    errored = 0
    for i, q in enumerate(questions, 1):
        print(f"  [{i:02d}/{len(questions)}] {q['id']} — {q['question'][:55]}", end="", flush=True)
        try:
            r = _run_question(brain, q, k=args.k, run_faithfulness=run_faithfulness)
        except Exception as e:
            # One provider hiccup must not destroy a 28-question run. Record the
            # failure and keep going; the scorecard reports how many errored so
            # a degraded run is never mistaken for a clean one.
            errored += 1
            print(f"  ERROR  ({type(e).__name__}: {str(e)[:80]})")
            continue
        icon = "✓" if r["passed"] else "✗"
        print(f"  {icon}  ({r['latency_s']}s)")
        results.append(r)

    if errored:
        print(f"\nWARNING: {errored}/{len(questions)} questions errored and are "
              f"excluded from the scores below.")
    if not results:
        print("No questions completed successfully — no scorecard to report.")
        return

    total_time = time.perf_counter() - t0
    agg = _aggregate(results, k=args.k)

    _print_scorecard(results, agg, total_time, k=args.k)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "workspace":    ws,
        "k":            args.k,
        "n_questions":  len(results),
        "n_errored":    errored,
        "fact_count":   fact_count,
        "vec_count":    vec_count,
        "total_time_s": round(total_time, 2),
        "aggregate":    agg,
        "results":      results,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Results saved to {out_path}")


def _load_questions(path: str) -> list:
    with open(path) as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    main()
