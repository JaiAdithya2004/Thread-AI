#!/usr/bin/env bash
# Run the full end-to-end benchmark: three synthetic runs, pooled, then (optionally)
# the real-mail track.
#
#   scripts/bench/run_e2e_benchmark.sh                      # synthetic x3 + pooled report
#   OWNER=<composio user id> scripts/bench/run_e2e_benchmark.sh   # ...and your own Gmail
#
# Needs a LOCAL Postgres (DATABASE_URL), a funded OpenRouter key, and takes roughly
# 8-10 minutes per synthetic run. A run that hit a provider outage is stamped
# INVALID and excluded from the pooled report; the script says so and exits non-zero.
set -u
cd "$(dirname "$0")/../.."
PY="${PYTHON:-.venv/Scripts/python}"
[ -x "$PY" ] || PY=python
OUT="evals/results/e2e_$(date +%Y-%m-%d)"
mkdir -p "$OUT"

rc=0
for i in 1 2 3; do
  echo "== synthetic run $i"
  BENCH_WS_SUFFIX="_r$i" "$PY" -m evals.e2e synthetic --out "$OUT/synthetic_run$i.json" || rc=$?
  [ "$rc" = 3 ] && { echo "preflight failed: fix the provider account and re-run"; exit 3; }
done

echo "== pooled (valid runs only)"
"$PY" -m evals.e2e aggregate "$OUT"/synthetic_run*.json --out "$OUT/synthetic_pooled.json" || rc=$?

if [ -n "${OWNER:-}" ]; then
  echo "== real mail (aggregates only are written to the public results file)"
  "$PY" -m evals.e2e real --owner "$OWNER" --days "${DAYS:-90}" --out "$OUT/real.json" || rc=$?
fi
exit $rc
