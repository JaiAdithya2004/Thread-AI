"""
bench_compare.py — Compare a new eval run against a saved baseline.

Usage:
  # Run a fresh eval and compare against baseline in one step:
  python scripts/bench_compare.py

  # Compare an already-captured results file:
  python scripts/bench_compare.py --results evals/results.json

  # Compare against a specific baseline:
  python scripts/bench_compare.py --baseline evals/baseline.json

  # Exit 1 if any regression found (for CI):
  python scripts/bench_compare.py --strict

Regressions are defined in evals/thresholds.yaml.
A regression is flagged when a metric drops MORE than its tolerance below
the baseline value, OR falls below the hard min_value floor.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml


THRESHOLDS_PATH = Path("evals/thresholds.yaml")
BASELINE_PATH   = Path("evals/baseline.json")


def _load_thresholds() -> dict:
    with open(THRESHOLDS_PATH) as f:
        return yaml.safe_load(f)["metrics"]


def _load_baseline(path: Path) -> dict:
    if not path.exists():
        print(f"[bench] No baseline found at {path}.", file=sys.stderr)
        print("[bench] Run:  python scripts/bench_snapshot.py  to create one.", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def _run_eval(ws: str, k: int, no_faith: bool, out: str) -> dict:
    cmd = [sys.executable, "-m", "evals.run", "--out", out, "--k", str(k)]
    if ws:
        cmd += ["--ws", ws]
    if no_faith:
        cmd += ["--no-faithfulness"]
    print(f"[bench] Running eval: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=Path(__file__).parent.parent)
    if result.returncode != 0:
        print("[bench] Eval run failed.", file=sys.stderr)
        sys.exit(1)
    with open(out) as f:
        return json.load(f)


def _compare(baseline: dict, current: dict, thresholds: dict) -> list:
    """
    Return a list of regression dicts.
    Each item: {metric, baseline_val, current_val, delta, threshold, min_value, kind}
    kind is "delta_regression" or "floor_violation"
    """
    regressions = []
    base_agg    = baseline.get("aggregate", {})
    curr_agg    = current.get("aggregate", {})

    for metric, cfg in thresholds.items():
        direction = cfg.get("direction", "higher_is_better")
        tolerance = cfg.get("tolerance", 0.10)
        min_value = cfg.get("min_value")

        # Normalise metric name for k-dependent keys
        k = current.get("k", 5)
        metric_key = metric.replace("_at_k", f"_at_{k}")

        base_val = base_agg.get(metric_key)
        curr_val = curr_agg.get(metric_key)

        if curr_val is None:
            continue   # metric not measured in this run (e.g. faithfulness skipped)

        # Hard floor check
        if min_value is not None:
            if direction == "higher_is_better" and curr_val < min_value:
                regressions.append({
                    "metric":       metric_key,
                    "baseline_val": base_val,
                    "current_val":  round(curr_val, 4),
                    "delta":        None,
                    "tolerance":    None,
                    "min_value":    min_value,
                    "kind":         "floor_violation",
                })
                continue

        if base_val is None:
            continue   # no baseline for this metric — skip delta check

        delta = curr_val - base_val   # positive = improved, negative = regressed

        if direction == "higher_is_better":
            if delta < -tolerance:
                regressions.append({
                    "metric":       metric_key,
                    "baseline_val": round(base_val, 4),
                    "current_val":  round(curr_val, 4),
                    "delta":        round(delta, 4),
                    "tolerance":    -tolerance,
                    "min_value":    min_value,
                    "kind":         "delta_regression",
                })
        else:  # lower_is_better (latency)
            if delta > tolerance:
                regressions.append({
                    "metric":       metric_key,
                    "baseline_val": round(base_val, 4),
                    "current_val":  round(curr_val, 4),
                    "delta":        round(delta, 4),
                    "tolerance":    tolerance,
                    "min_value":    min_value,
                    "kind":         "delta_regression",
                })

    return regressions


def _per_question_regressions(baseline: dict, current: dict) -> list:
    """Flag questions that newly started failing (passed in baseline, fail now)."""
    base_pq = baseline.get("per_question", {})
    curr_results = {r["id"]: r for r in current.get("results", [])}
    regressions = []
    for qid, base_r in base_pq.items():
        curr_r = curr_results.get(qid)
        if curr_r is None:
            continue
        if base_r.get("passed") and not curr_r.get("passed"):
            regressions.append({
                "question_id":   qid,
                "baseline_passed": True,
                "current_passed":  False,
            })
    return regressions


def _print_report(baseline: dict, current: dict,
                  metric_regressions: list, question_regressions: list):
    W = 68
    k = current.get("k", 5)
    base_agg = baseline.get("aggregate", {})
    curr_agg = current.get("aggregate", {})

    print("\n" + "═" * W)
    print("  Thread Regression Report")
    print("═" * W)

    base_meta = baseline.get("meta", {})
    print(f"\n  Baseline:  {base_meta.get('label', '?')}  "
          f"captured {base_meta.get('captured_at', '?')[:19]}")
    print(f"  Current:   {current.get('workspace', '?')}  "
          f"{current.get('fact_count', '?')} facts")

    all_metrics = [
        (f"hit_at_{k}",           "higher"),
        (f"precision_at_{k}",     "higher"),
        (f"recall_at_{k}",        "higher"),
        ("mrr",                   "higher"),
        (f"ndcg_at_{k}",          "higher"),
        ("answer_correct_rate",   "higher"),
        ("keyword_hit_rate",      "higher"),
        ("faithfulness_rate",     "higher"),
        ("abstention_accuracy",   "higher"),
        ("mean_latency_s",        "lower"),
        ("p95_latency_s",         "lower"),
    ]

    regressed_keys = {r["metric"] for r in metric_regressions}

    print(f"\n  {'METRIC':<30} {'BASELINE':>10} {'CURRENT':>10} {'DELTA':>8}  STATUS")
    print("  " + "─" * (W - 2))

    for key, direction in all_metrics:
        bv = base_agg.get(key)
        cv = curr_agg.get(key)
        if bv is None and cv is None:
            continue
        bv_str = f"{bv:.3f}" if bv is not None else "N/A"
        cv_str = f"{cv:.3f}" if cv is not None else "N/A"
        if bv is not None and cv is not None:
            delta = cv - bv
            delta_str = f"{delta:+.3f}"
            if key in regressed_keys:
                status = "✗ REGRESSED"
            elif (direction == "higher" and delta > 0.005) or (direction == "lower" and delta < -0.005):
                status = "↑ improved"
            elif (direction == "higher" and delta < -0.005) or (direction == "lower" and delta > 0.005):
                status = "↓ degraded (within tolerance)"
            else:
                status = "  stable"
        else:
            delta_str = "N/A"
            status = "N/A"
        print(f"  {key:<30} {bv_str:>10} {cv_str:>10} {delta_str:>8}  {status}")

    if question_regressions:
        print(f"\n  NEWLY FAILING QUESTIONS  ({len(question_regressions)})")
        for r in question_regressions:
            print(f"    ✗ {r['question_id']}  (was passing, now failing)")

    if metric_regressions:
        print(f"\n  METRIC REGRESSIONS  ({len(metric_regressions)})")
        for r in metric_regressions:
            if r["kind"] == "floor_violation":
                print(f"    ✗ {r['metric']:<30} current={r['current_val']}  "
                      f"below hard floor {r['min_value']}")
            else:
                print(f"    ✗ {r['metric']:<30} {r['baseline_val']} → {r['current_val']}  "
                      f"(delta={r['delta']}, tolerance={r['tolerance']})")
    else:
        print(f"\n  ✓  No metric regressions detected.")

    total_regressions = len(metric_regressions) + len(question_regressions)
    print(f"\n  {'RESULT: FAIL' if total_regressions else 'RESULT: PASS'}  "
          f"({total_regressions} regression(s))")
    print("═" * W + "\n")

    return total_regressions


def main():
    parser = argparse.ArgumentParser(description="Thread regression benchmark")
    parser.add_argument("--baseline",  default="evals/baseline.json")
    parser.add_argument("--results",   default=None,
                        help="Path to an already-run results.json — skips running eval")
    parser.add_argument("--out",       default="evals/results_compare.json")
    parser.add_argument("--ws",        default=None)
    parser.add_argument("--k",         type=int, default=5)
    parser.add_argument("--no-faithfulness", action="store_true")
    parser.add_argument("--strict",    action="store_true",
                        help="Exit 1 if any regression found (for CI)")
    args = parser.parse_args()

    thresholds = _load_thresholds()
    baseline   = _load_baseline(Path(args.baseline))

    if args.results:
        with open(args.results) as f:
            current = json.load(f)
    else:
        current = _run_eval(args.ws, args.k, args.no_faithfulness, args.out)

    metric_regressions   = _compare(baseline, current, thresholds)
    question_regressions = _per_question_regressions(baseline, current)

    total = _print_report(baseline, current, metric_regressions, question_regressions)

    # Save comparison output
    out_path = Path(args.out)
    comparison = {
        "metric_regressions":   metric_regressions,
        "question_regressions": question_regressions,
        "total_regressions":    total,
        "current":              current.get("aggregate"),
        "baseline":             baseline.get("aggregate"),
    }
    with open(out_path, "w") as f:
        json.dump(comparison, f, indent=2)
    print(f"Comparison saved to {out_path}")

    if args.strict and total > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
