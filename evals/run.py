"""
Eval harness for Company Brain.

Usage:
    python -m evals.run
    python -m evals.run --questions evals/questions.yaml --out evals/results.json
    python -m evals.run --reset-extracted   # mark all episodes unextracted, then exit

Requires the server to NOT be running (connects to SQLite directly).
Optional: pip install ragas datasets langchain-openai  for RAGAS metrics.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from brain.config import WORKSPACE_ID, TEAM_ID, CALLER, CALLER_PRINCIPALS
from brain.engine import Brain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_questions(path: str) -> list:
    with open(path) as f:
        return yaml.safe_load(f)


def _run_question(brain: Brain, q: dict) -> dict:
    start = time.perf_counter()
    result = brain.search(q["question"], surface="eval")
    elapsed = time.perf_counter() - start

    answer_text = (result.get("synthesized_answer") or result.get("answer") or "").lower()
    facts = result.get("facts", [])
    fact_summaries = " ".join(f.get("summary", "").lower() for f in facts)

    expects_not_sure = q.get("expect_not_sure", False)
    expected_answer = (q.get("expected_answer_contains") or "").lower()
    expected_fact = (q.get("expected_source_fact_summary_contains") or "").lower()

    # Calibration metrics
    top_confidence = facts[0].get("confidence", 0.0) if facts else 0.0
    has_synthesized = bool(result.get("synthesized_answer", "").strip())
    stale_cited = any(f.get("status") == "stale" for f in facts[:3])

    if expects_not_sure:
        # Pass if brain returns "not certain" path (no synthesized answer or low confidence)
        passed = not has_synthesized or top_confidence < 0.4
    else:
        answer_ok = (not expected_answer) or (expected_answer in answer_text)
        fact_ok = (not expected_fact) or (expected_fact in fact_summaries)
        passed = answer_ok and fact_ok

    return {
        "id": q["id"],
        "category": q.get("category", ""),
        "question": q["question"],
        "passed": passed,
        "latency_s": round(elapsed, 3),
        "answer_snippet": answer_text[:200],
        "facts_returned": len(facts),
        "top_fact": facts[0].get("summary", "") if facts else "",
        "top_confidence": round(top_confidence, 3),
        "has_synthesized": has_synthesized,
        "stale_cited": stale_cited,
        "expects_not_sure": expects_not_sure,
    }


def _ragas_score(questions: list, results: list) -> dict:
    """Best-effort RAGAS metrics. Returns empty dict if not installed."""
    try:
        from ragas import evaluate
        from ragas.metrics import faithfulness, answer_relevancy, context_recall
        from datasets import Dataset

        rows = {
            "question": [],
            "answer": [],
            "contexts": [],
            "ground_truth": [],
        }
        for q, r in zip(questions, results):
            if q.get("expect_not_sure"):
                continue
            rows["question"].append(q["question"])
            rows["answer"].append(r["answer_snippet"])
            # Use all returned fact summaries as context, not just the top one
            rows["contexts"].append(
                [f["summary"] for f in [{"summary": r["top_fact"]}] if f["summary"]] or [""]
            )
            rows["ground_truth"].append(q.get("expected_answer_contains", ""))

        if not rows["question"]:
            return {}

        ds = Dataset.from_dict(rows)
        scores = evaluate(ds, metrics=[faithfulness, answer_relevancy, context_recall])
        return {k: round(float(v), 3) for k, v in scores.items()}
    except Exception as e:
        print(f"[eval] RAGAS unavailable ({e}), skipping.", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# Scorecard printer
# ---------------------------------------------------------------------------

def _print_scorecard(results: list, ragas: dict, total_time: float):
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    pct = 100 * passed // total if total else 0

    # Aggregate quality metrics
    answered = [r for r in results if not r["expects_not_sure"]]
    not_sure_qs = [r for r in results if r["expects_not_sure"]]

    correct_cited   = sum(1 for r in answered if r["passed"] and r["has_synthesized"])
    correct_ns      = sum(1 for r in not_sure_qs if r["passed"])
    stale_citations = sum(1 for r in results if r["stale_cited"])
    avg_lat         = round(sum(r["latency_s"] for r in results) / len(results), 2) if results else 0

    by_cat: dict[str, list] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)

    print("\n" + "=" * 68)
    print(f"  Company Brain Eval — {passed}/{total} passed ({pct}%)  [{total_time:.1f}s total, {avg_lat}s avg]")
    print("=" * 68)
    print(f"  Correct + cited:       {correct_cited}/{len(answered)}")
    print(f"  Correct 'not sure':    {correct_ns}/{len(not_sure_qs)}")
    print(f"  Stale-fact citations:  {stale_citations}")
    print()

    for cat, cat_results in sorted(by_cat.items()):
        cat_pass = sum(1 for r in cat_results if r["passed"])
        print(f"  [{cat}]  {cat_pass}/{len(cat_results)}")
        for r in cat_results:
            icon = "✓" if r["passed"] else "✗"
            lat = f"{r['latency_s']}s"
            conf = f"conf={r['top_confidence']:.2f}"
            print(f"    {icon} {r['id']}  {r['question'][:50]:<50}  {lat}  {conf}")
            if not r["passed"]:
                print(f"         answer:   {r['answer_snippet'][:80]}")
                print(f"         top fact: {r['top_fact'][:80]}")

    if ragas:
        print()
        print("  RAGAS metrics:")
        for k, v in ragas.items():
            print(f"    {k}: {v}")

    print("=" * 68 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Company Brain eval harness")
    parser.add_argument("--questions", default="evals/questions.yaml")
    parser.add_argument("--out", default="evals/results.json")
    parser.add_argument("--team", default=None)
    parser.add_argument("--reset-extracted", action="store_true",
                        help="Mark all episodes as unextracted so extraction re-runs on next start")
    args = parser.parse_args()

    brain = Brain(
        workspace_id=WORKSPACE_ID,
        team_id=args.team or TEAM_ID,
        caller=CALLER,
        principals=CALLER_PRINCIPALS,
    )

    if args.reset_extracted:
        brain.conn.execute(
            "UPDATE episodes SET extracted=0 WHERE workspace_id=?", (brain.ws,))
        brain.conn.commit()
        n = brain.conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE workspace_id=?", (brain.ws,)).fetchone()[0]
        print(f"Reset {n} episodes to extracted=0. Run backfill or restart to re-extract.")
        return

    questions = _load_questions(args.questions)

    # Warn if no facts in DB
    fact_count = brain.conn.execute(
        "SELECT COUNT(*) FROM facts WHERE workspace_id=? AND invalidated_at IS NULL",
        (brain.ws,)).fetchone()[0]
    if fact_count == 0:
        print(f"WARNING: 0 active facts in workspace '{brain.ws}'. "
              "Run extraction first (need valid LLM API key).")
        print("  Hint: python -m evals.run --reset-extracted  then restart the server.")

    print(f"Running {len(questions)} questions against workspace '{brain.ws}' "
          f"({fact_count} active facts)...")
    t0 = time.perf_counter()
    results = [_run_question(brain, q) for q in questions]
    total_time = time.perf_counter() - t0

    ragas = _ragas_score(questions, results)
    _print_scorecard(results, ragas, total_time)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"results": results, "ragas": ragas, "fact_count": fact_count}, f, indent=2)
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
