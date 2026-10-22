"""Combine several evals.bench runs into the tables published in the README.

    python -m evals.aggregate                    # print markdown tables
    python -m evals.aggregate --write-baseline   # also refresh evals/baseline.json

The LLM-judged parts (answers, supersession, end-to-end) are not deterministic,
so a single run can flatter or punish the system. Report the mean over several
runs together with the range, and pool the underlying counts.
"""
import argparse
import glob
import json
from pathlib import Path

HERE = Path(__file__).parent


def load(pattern: str) -> list:
    return [json.loads(Path(p).read_text(encoding="utf-8"))
            for p in sorted(glob.glob(str(HERE / pattern)))]


def dig(d, path):
    for p in path:
        d = d[p]
    return d


def cell(runs, path) -> str:
    """'0.938 (0.93-0.94)  42/45'  for proportions, 'x (min-max)' for plain numbers."""
    vals = [dig(r, path) for r in runs]
    if all(isinstance(v, dict) and "k" in v for v in vals):
        ks, ns = sum(v["k"] for v in vals), sum(v["n"] for v in vals)
        xs = [v["value"] for v in vals if v["value"] is not None]
        if not ns:
            return "n/a"
        rng = f"{min(xs):.2f}-{max(xs):.2f}" if len(set(xs)) > 1 else "all runs equal"
        return f"**{ks / ns:.3f}** ({ks}/{ns}; {rng})"
    xs = [v for v in vals if v is not None]
    if not xs:
        return "n/a"
    m = sum(xs) / len(xs)
    if len(set(xs)) > 1:
        return f"**{m:.3f}** ({min(xs):.2f}-{max(xs):.2f})"
    return f"**{m:.3f}**"


def num(runs, path):
    vals = [dig(r, path) for r in runs]
    if vals and isinstance(vals[0], dict) and "k" in vals[0]:
        ks, ns = sum(v["k"] for v in vals), sum(v["n"] for v in vals)
        return round(ks / ns, 4) if ns else None
    xs = [v for v in vals if v is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def table(title, header, rows) -> str:
    out = [f"**{title}**", "", "| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="results/bench_run_*.json")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--tables-out", default="results/tables.md",
                    help="Where to write the markdown tables (relative to evals/).")
    args = ap.parse_args()
    runs = load(args.glob)
    if not runs:
        raise SystemExit("no runs found")
    meta = runs[-1]["meta"]
    out = [f"_{len(runs)} runs, {meta['date']}, commit `{meta['git']}`. Mean over runs; range in "
           f"parentheses; counts are pooled across runs._\n"]

    if "A" in runs[0]:
        rows = []
        for r in ("vector", "fulltext", "hybrid_rrf", "production"):
            p = ("A", "retrieval", r)
            rows.append([r, cell(runs, p + ("hit@5",)), cell(runs, p + ("recall@5",)),
                         cell(runs, p + ("precision@5",)), cell(runs, p + ("mrr",)),
                         cell(runs, p + ("ndcg@5",))])
        out.append(table("Retrieval (k=5)", ["Ranker", "Hit@5", "Recall@5", "Precision@5", "MRR", "nDCG@5"], rows))
        if "answers" in runs[0]["A"]:
            a = ("A", "answers")
            rows = [[n, cell(runs, a + (k,))] for n, k in (
                ("Answer correct (answerable)", "answer_correct"),
                ("Faithfulness (LLM judge)", "faithfulness"),
                ("Citation validity (cited id was retrieved)", "citation_validity"),
                ("Citation support (LLM judge)", "citation_support"),
                ("Abstention on unanswerable", "abstention"))]
            rows.append(["Answer latency p50 / p95 (s)",
                         f"{num(runs, a + ('latency_s', 'p50'))} / {num(runs, a + ('latency_s', 'p95'))}"])
            rows.append(["Retrieval latency p50 / p95 (s)",
                         f"{num(runs, ('A', 'retrieval_latency_s', 'p50'))} / "
                         f"{num(runs, ('A', 'retrieval_latency_s', 'p95'))}"])
            out.append(table("Answer quality", ["Metric", "Result"], rows))

    if "B" in runs[0]:
        labels = {"legacy_exact_subject": "Exact-subject (previous)",
                  "embedding_only": "Embedding similarity only",
                  "reconcile": "Embedding + LLM judge (current)"}
        mets = [("Supersession precision", "supersession_precision"),
                ("Supersession recall", "supersession_recall"),
                ("Supersession F1", "supersession_f1"),
                ("Updates lost as 'duplicates'", "updates_dropped_as_duplicate"),
                ("Paraphrases recognised as duplicates", "duplicate_detection"),
                ("Decoys wrongly acted on", "decoy_false_actions"),
                ("Future-dated changes wrongly superseded", "future_dated_wrongly_superseded")]
        rows = [[name] + [cell(runs, ("B", "methods", m, key)) for m in labels] for name, key in mets]
        rows.append(["True predecessor among candidates", "-", "-",
                     cell(runs, ("B", "candidate_recall_of_true_predecessor"))])
        out.append(table("Supersession decisions (pairwise, database in true state)",
                         ["Metric"] + list(labels.values()), rows))

    if "C" in runs[0]:
        mets = [("Versions stored", "versions_stored"),
                ("Version status correct (valid vs stale)", "version_status_correct"),
                ("Chains fully correct", "chains_fully_correct"),
                ("'Current X?' top result is the latest", "current_top1_is_latest"),
                ("'Current X?' answer states latest only", "current_answer_latest_only"),
                ("History returns every version", "history_recall"),
                ("History marks stale/valid correctly", "history_status_correct")]
        rows = [[name, cell(runs, ("C", "legacy", k)), cell(runs, ("C", "reconcile", k))]
                for name, k in mets]
        rows.append(["Unrelated facts wrongly invalidated",
                     str(sum(r["C"]["legacy"]["base_facts_wrongly_invalidated"] for r in runs)),
                     str(sum(r["C"]["reconcile"]["base_facts_wrongly_invalidated"] for r in runs))])
        out.append(table("End to end: changing facts through the real save pipeline",
                         ["Metric", "Reconciliation off (previous)", "Reconciliation on (current)"], rows))

    text = "\n".join(out)
    print(text)
    (HERE / args.tables_out).write_text(text, encoding="utf-8")

    if args.write_baseline:
        agg = {}
        for r in ("vector", "hybrid_rrf", "production"):
            for k in ("hit@5", "recall@5", "precision@5", "mrr", "ndcg@5"):
                agg[f"retrieval.{r}.{k}"] = num(runs, ("A", "retrieval", r, k))
        if "answers" in runs[0]["A"]:
            for k in ("answer_correct", "faithfulness", "citation_validity",
                      "citation_support", "abstention"):
                agg[f"answers.{k}"] = num(runs, ("A", "answers", k))
        for k in ("supersession_precision", "supersession_recall", "supersession_f1",
                  "duplicate_detection", "decoy_false_actions"):
            agg[f"supersession.reconcile.{k}"] = num(runs, ("B", "methods", "reconcile", k))
        for k in ("chains_fully_correct", "current_top1_is_latest", "current_answer_latest_only"):
            agg[f"end_to_end.reconcile.{k}"] = num(runs, ("C", "reconcile", k))
        base = {"meta": {**meta, "n_runs": len(runs)}, "aggregate": agg}
        (HERE / "baseline.json").write_text(json.dumps(base, indent=2), encoding="utf-8")
        print("\nwrote evals/baseline.json")


if __name__ == "__main__":
    main()
