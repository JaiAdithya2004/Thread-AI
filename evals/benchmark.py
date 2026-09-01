"""
Trustworthy retrieval benchmark.

Distinct from evals/run.py: this scores retrieval ONLY, against explicit
ground truth in evals/ground_truth.yaml. It never asks an LLM anything, so
provider rate limits cannot corrupt a run, and it never derives relevance
from the system's own output.

Design rules this file exists to enforce:

  1. The workspace is explicit. No default. Running without --workspace is an
     error, because silently benchmarking whichever workspace happened to be
     configured is what produced the previous invalid baseline.

  2. Ground truth is independent of retrieval. relevant_ids come from
     ground_truth.yaml only. The metric functions receive them as an argument
     and cannot see the retrieved list when constructing them.

  3. K is consistent. Retrieval is asked for exactly K facts and scored at K.
     The previous harness let production return 8 while scoring at 5, so a
     relevant fact at rank 6-8 counted in the recall denominator but could
     never count in the numerator.

  4. Unanswerable questions are scored on abstention, not converted into
     retrieval failures. Their retrieval metrics are None, not 0.0.

Usage:
    python -m evals.benchmark --workspace ws_local
    python -m evals.benchmark --workspace ws_local --k 5
    python -m evals.benchmark --workspace ws_local --exclude-fact <id>   # negative control
"""
import argparse
import json
import math
import sys
from pathlib import Path

import yaml

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── Metrics ───────────────────────────────────────────────────────────────────
# Each takes retrieved_ids (from the system) and relevant_ids (from ground
# truth) as separate arguments. Neither derives one from the other.

def hit_at_k(retrieved_ids, relevant_ids, k):
    rel = set(relevant_ids)
    return float(any(fid in rel for fid in retrieved_ids[:k]))


def precision_at_k(retrieved_ids, relevant_ids, k):
    if not retrieved_ids:
        return 0.0
    rel = set(relevant_ids)
    top = retrieved_ids[:k]
    return sum(1 for fid in top if fid in rel) / len(top)


def recall_at_k(retrieved_ids, relevant_ids, k):
    """Denominator is ALL relevant facts, whether or not retrieval found them.

    This is the property the old harness lacked: a missed fact stayed out of
    the denominator, so recall could not fall.
    """
    if not relevant_ids:
        return None
    rel = set(relevant_ids)
    return sum(1 for fid in retrieved_ids[:k] if fid in rel) / len(rel)


def mrr(retrieved_ids, relevant_ids):
    rel = set(relevant_ids)
    for rank, fid in enumerate(retrieved_ids, start=1):
        if fid in rel:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids, relevant_ids, k):
    if not relevant_ids:
        return None
    rel = set(relevant_ids)
    dcg = sum(1.0 / math.log2(i + 2)
              for i, fid in enumerate(retrieved_ids[:k]) if fid in rel)
    # Ideal: every relevant fact packed into the top ranks, capped at k.
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(rel), k)))
    return dcg / ideal if ideal > 0 else None


# ── Runner ────────────────────────────────────────────────────────────────────

def load_ground_truth(path):
    gt = yaml.safe_load(open(path, encoding="utf-8"))
    alias = gt["facts"]
    resolved = {}
    for qid, spec in gt["questions"].items():
        ids = [alias[name] for name in spec.get("relevant", [])]
        resolved[qid] = {
            "answerable": spec["answerable"],
            "relevant":   ids,
            "note":       spec.get("note", ""),
        }
    return gt, resolved


def main():
    p = argparse.ArgumentParser(description="Retrieval benchmark (retrieval only)")
    # No default: an unspecified workspace must fail loudly.
    p.add_argument("--workspace", required=True,
                   help="Workspace to benchmark. Required — there is no default.")
    p.add_argument("--k", type=int, default=5,
                   help="Retrieve and score at this K (default 5).")
    p.add_argument("--questions",    default="evals/questions.yaml")
    p.add_argument("--ground-truth", default="evals/ground_truth.yaml")
    p.add_argument("--out",          default="evals/benchmark_results.json")
    p.add_argument("--exclude-fact", default=None,
                   help="Negative control: drop this fact id from retrieval output.")
    args = p.parse_args()

    gt_doc, gt = load_ground_truth(args.ground_truth)

    expected_ws = gt_doc.get("workspace")
    if expected_ws and args.workspace != expected_ws:
        print(f"ERROR: ground truth is defined for workspace '{expected_ws}' but "
              f"--workspace={args.workspace} was given.\n"
              f"Scoring a different workspace against this ground truth is "
              f"meaningless — that is the bug this benchmark exists to prevent.",
              file=sys.stderr)
        return 2

    questions = yaml.safe_load(open(args.questions, encoding="utf-8"))
    from brain.core import db as _db, retrieve

    conn = _db.connect()
    ws, k = args.workspace, args.k
    rows = []

    print(f"workspace={ws}  k={k}  questions={len(questions)}")
    if args.exclude_fact:
        print(f"NEGATIVE CONTROL: excluding fact {args.exclude_fact} from retrieval")
    print()

    for q in questions:
        qid = q["id"]
        truth = gt.get(qid)
        if truth is None:
            print(f"  {qid}  SKIP (no ground truth entry)")
            continue

        facts = retrieve.run(
            conn, ws, q["question"],
            principals=["everyone"], caller="benchmark@eval",
            surface="eval", team_id=None, cross_team=True,
            top=k,                      # ask for exactly K — no 8-vs-5 mismatch
        )
        retrieved = [f["id"] for f in facts]
        if args.exclude_fact:
            retrieved = [i for i in retrieved if not i.startswith(args.exclude_fact)]

        relevant = truth["relevant"]
        answerable = truth["answerable"]

        if answerable:
            rec = {
                "hit":       hit_at_k(retrieved, relevant, k),
                "precision": precision_at_k(retrieved, relevant, k),
                "recall":    recall_at_k(retrieved, relevant, k),
                "mrr":       mrr(retrieved, relevant),
                "ndcg":      ndcg_at_k(retrieved, relevant, k),
            }
            ranks = {rid[:8]: (retrieved.index(rid) + 1 if rid in retrieved else None)
                     for rid in relevant}
            flag = "OK " if rec["hit"] == 1.0 else "MISS"
        else:
            # Unanswerable: retrieval metrics are not applicable. Abstention is
            # measured as "did the system return nothing above threshold".
            rec = {"hit": None, "precision": None, "recall": None,
                   "mrr": None, "ndcg": None}
            ranks = {}
            flag = "ABST" if not retrieved else "----"

        rows.append({
            "id": qid, "question": q["question"], "answerable": answerable,
            "retrieved": [r[:8] for r in retrieved],
            "relevant":  [r[:8] for r in relevant],
            "ranks": ranks, "n_retrieved": len(retrieved), **rec,
        })
        print(f"  {flag} {qid}  ret={len(retrieved)}  "
              f"hit={rec['hit']}  recall={rec['recall']}  ranks={ranks or '-'}")

    conn.close()

    ans = [r for r in rows if r["answerable"]]
    una = [r for r in rows if not r["answerable"]]

    def avg(key, src):
        vals = [r[key] for r in src if r[key] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    agg = {
        "workspace": ws, "k": k,
        "n_questions": len(rows),
        "n_answerable": len(ans), "n_unanswerable": len(una),
        f"hit_at_{k}":       avg("hit", ans),
        f"precision_at_{k}": avg("precision", ans),
        f"recall_at_{k}":    avg("recall", ans),
        "mrr":               avg("mrr", ans),
        f"ndcg_at_{k}":      avg("ndcg", ans),
        "abstention_rate": round(
            sum(1 for r in una if r["n_retrieved"] == 0) / len(una), 4) if una else None,
        "excluded_fact": args.exclude_fact,
    }

    print("\n" + "=" * 62)
    for kk, vv in agg.items():
        print(f"  {kk:22} {vv}")
    print("=" * 62)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump({"aggregate": agg, "results": rows},
              open(args.out, "w", encoding="utf-8"), indent=2)
    print(f"saved -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
