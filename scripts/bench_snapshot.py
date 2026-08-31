"""
bench_snapshot.py — Capture the current eval results as the regression baseline.

Run this after any architectural change you want to set as the new quality bar:
  python scripts/bench_snapshot.py
  python scripts/bench_snapshot.py --ws ws_demo --out evals/baseline.json

The snapshot file is committed to git so future runs can compare against it.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Capture eval baseline snapshot")
    parser.add_argument("--ws",  default=None, help="Workspace (default: BRAIN_WORKSPACE env)")
    parser.add_argument("--out", default="evals/baseline.json", help="Output path")
    parser.add_argument("--k",   type=int, default=5)
    parser.add_argument("--no-faithfulness", action="store_true",
                        help="Skip LLM-as-judge (faster, but faithfulness not captured)")
    parser.add_argument("--label", default=None,
                        help="Human label for this snapshot (e.g. 'after-vec-fix')")
    args = parser.parse_args()

    # Build the eval command
    cmd = [sys.executable, "-m", "evals.run",
           "--out", "evals/_snapshot_tmp.json",
           "--k", str(args.k)]
    if args.ws:
        cmd += ["--ws", args.ws]
    if args.no_faithfulness:
        cmd += ["--no-faithfulness"]

    print(f"[bench] Running eval: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=Path(__file__).parent.parent)
    if result.returncode != 0:
        print("[bench] Eval run failed — baseline NOT updated.", file=sys.stderr)
        sys.exit(1)

    # Load the raw results
    tmp = Path("evals/_snapshot_tmp.json")
    with open(tmp) as f:
        data = json.load(f)
    tmp.unlink(missing_ok=True)

    # Build snapshot with metadata
    import importlib.metadata
    try:
        litellm_ver = importlib.metadata.version("litellm")
    except Exception:
        litellm_ver = "unknown"

    snapshot = {
        "meta": {
            "captured_at":    datetime.now(timezone.utc).isoformat(),
            "label":          args.label or "baseline",
            "workspace":      data.get("workspace"),
            "k":              data.get("k"),
            "fact_count":     data.get("fact_count"),
            "vec_count":      data.get("vec_count"),
            "litellm_version": litellm_ver,
        },
        "aggregate": data.get("aggregate", {}),
        "per_question": {
            r["id"]: {
                "passed":          r["passed"],
                "hit_at_k":        r.get("hit_at_k"),
                "precision_at_k":  r.get("precision_at_k"),
                "recall_at_k":     r.get("recall_at_k"),
                "mrr":             r.get("mrr"),
                "ndcg_at_k":       r.get("ndcg_at_k"),
                "answer_correct":  r.get("answer_correct"),
                "keyword_hit":     r.get("keyword_hit"),
                "faithful":        r.get("faithful"),
                "abstention_correct": r.get("abstention_correct"),
                "latency_s":       r.get("latency_s"),
            }
            for r in data.get("results", [])
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2)

    agg = snapshot["aggregate"]
    print(f"\n[bench] Baseline saved to {out_path}")
    print(f"        label:        {snapshot['meta']['label']}")
    print(f"        captured_at:  {snapshot['meta']['captured_at']}")
    print(f"        fact_count:   {snapshot['meta']['fact_count']}")
    print(f"        hit@{args.k}:       {agg.get(f'hit_at_{args.k}')}")
    print(f"        answer_correct: {agg.get('answer_correct_rate')}")
    print(f"        faithfulness:   {agg.get('faithfulness_rate')}")
    print(f"        abstention:     {agg.get('abstention_accuracy')}")
    print(f"\n[bench] Commit {out_path} to git to lock in this baseline.")


if __name__ == "__main__":
    main()
