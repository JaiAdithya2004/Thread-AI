"""Validate Thread on a team's REAL questions and decisions.

The controlled benchmark (evals/bench.py) shows the pipeline behaves correctly on
a fictional company. This tool measures it on actual use. Nothing here is run
automatically: it needs a workspace with real data and a person to label it.

    # 1. Draw questions that were really asked, for a teammate to label
    python -m evals.real_eval sample --ws WS --team TEAM --n 100 --out labels.yaml

    # 2. (human) fill in `gold:` fact ids for each question, or set answerable: false

    # 3. Score retrieval and answers on the labelled set, with 95% intervals
    python -m evals.real_eval score --ws WS --team TEAM --labels labels.yaml --out real_results.json

    # 4. Sample supersession decisions for human review ...
    python -m evals.real_eval audit-sample --ws WS --n 100 --out audit.csv
    #    (or, after a shadow-mode trial, what reconcile WOULD have done:)
    python -m evals.real_eval audit-sample --ws WS --n 100 --source shadow --out audit.csv

    # 5. (human) fill the `verdict` column with correct / wrong / unsure, then:
    python -m evals.real_eval audit-score --file audit.csv

Privacy: private (connector-owned) facts are never exported for labelling, and
`--redact` masks emails, URLs and long numbers in exported text.

Shadow mode: run the application with BRAIN_RECONCILE_MODE=shadow and every
reconcile decision is logged to `reconcile_shadow_log` without being applied.
"""
import argparse
import csv
import json
import os
import random
import re
import sys
from pathlib import Path

import yaml

from evals.bench import K, _ABSTAIN_RE, judge_faithful, citation_scores, mean, prop, rank_metrics

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Starting thresholds from the README's validation protocol; revise with real data.
PRECISION_FLOOR = 0.95
RETRIEVAL_FLOOR = 0.90

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL = re.compile(r"https?://\S+")
_NUMBER = re.compile(r"\b\d{6,}\b")


def redact(text: str) -> str:
    """Mask emails, URLs and long digit runs before text leaves the database."""
    text = _EMAIL.sub("<email>", text or "")
    text = _URL.sub("<url>", text)
    return _NUMBER.sub("<number>", text)


def _connect(ws: str):
    from brain.core import db, db_pg
    conn = db.connect()
    db.migrate(conn)
    db_pg._set_ws(conn, ws)
    return conn


# ── 1. sample questions for labelling ────────────────────────────────────────

def sample_questions(conn, ws, team, n, seed, do_redact):
    cur = conn.cursor()
    sql = ("SELECT query, fact_ids, ts FROM ledger WHERE workspace_id=%s "
           "AND surface IN ('web','api','mcp','slack') AND query IS NOT NULL")
    args = [ws]
    if team:
        sql += " AND team_id=%s"
        args.append(team)
    cur.execute(sql + " ORDER BY id DESC LIMIT 5000", args)
    rows = cur.fetchall()

    seen, pool = set(), []
    for r in rows:                       # one entry per distinct question
        key = " ".join((r["query"] or "").lower().split())
        if key and key not in seen:
            seen.add(key)
            pool.append(r)
    random.Random(seed).shuffle(pool)

    out = []
    for i, r in enumerate(pool[:n], 1):
        try:
            ids = json.loads(r["fact_ids"] or "[]")
        except Exception:
            ids = []
        returned = []
        if ids:
            cur.execute("SELECT id, summary FROM facts WHERE workspace_id=%s AND id = ANY(%s) "
                        "AND visibility <> 'private'", (ws, ids))
            returned = [{"id": f["id"], "summary": redact(f["summary"]) if do_redact else f["summary"]}
                        for f in cur.fetchall()]
        q = r["query"].strip()
        out.append({"id": f"r{i:03d}", "question": redact(q) if do_redact else q,
                    "answerable": True, "gold": [], "returned_then": returned, "notes": ""})
    cur.close()
    return out


# ── 3. score a labelled set ──────────────────────────────────────────────────

def score_labelled(entries, search_fn, with_answers=False, k=K):
    """Score labelled questions. `search_fn(question)` returns Brain.search output."""
    ranking, answers, abst = [], [], []
    unlabelled = 0
    cites = {"total": 0, "valid": 0, "judged": 0, "supported": 0}
    faith_k = faith_n = correct = 0
    for e in entries:
        answerable = e.get("answerable", True)
        gold = [str(g) for g in (e.get("gold") or [])]
        if answerable and not gold:
            unlabelled += 1
            continue
        res = search_fn(e["question"])
        facts = res.get("facts", [])
        text = (res.get("synthesized_answer") or "").strip()
        if answerable:
            ranking.append(rank_metrics([f["fact_id"] for f in facts], gold, k))
            if with_answers and text:
                f = judge_faithful(e["question"], text, facts)
                if f is not None:
                    faith_n += 1
                    faith_k += bool(f)
                c = citation_scores(text, facts)
                for key in cites:
                    cites[key] += c[key]
        else:
            refused = (not facts) or bool(_ABSTAIN_RE.search(text)) or \
                "reliable information" in (res.get("answer") or "")
            abst.append(bool(refused))

    out = {
        "n_labelled_answerable": len(ranking),
        "n_unanswerable": len(abst),
        "n_skipped_unlabelled": unlabelled,
        "k": k,
        "retrieval": {
            "hit@k": prop(int(sum(m["hit"] for m in ranking)), len(ranking)),
            "recall@k": mean([m["recall"] for m in ranking]),
            "precision@k": mean([m["precision"] for m in ranking]),
            "mrr": mean([m["mrr"] for m in ranking]),
            "ndcg@k": mean([m["ndcg"] for m in ranking]),
        },
        "abstention": prop(sum(abst), len(abst)),
    }
    if with_answers:
        out["answers"] = {
            "faithfulness": prop(faith_k, faith_n),
            "citation_validity": prop(cites["valid"], cites["total"]),
            "citation_support": prop(cites["supported"], cites["judged"]),
        }
    return out


# ── 4/5. audit supersession decisions ────────────────────────────────────────

AUDIT_COLUMNS = ["edge_id", "type", "newer_fact", "older_fact", "verdict", "notes"]


def audit_rows_from_edges(conn, ws, n, seed, do_redact):
    cur = conn.cursor()
    cur.execute("""
        SELECT fe.id, fe.type, fa.summary AS newer, fb.summary AS older
        FROM fact_edges fe
        JOIN facts fa ON fa.id = fe.src_fact AND fa.workspace_id = fe.workspace_id
        JOIN facts fb ON fb.id = fe.dst_fact AND fb.workspace_id = fe.workspace_id
        WHERE fe.workspace_id = %s AND fe.type IN ('supersedes', 'in_tension_with')
          AND fa.visibility <> 'private' AND fb.visibility <> 'private'
    """, (ws,))
    rows = cur.fetchall()
    cur.close()
    random.Random(seed).shuffle(rows)
    f = redact if do_redact else (lambda x: x)
    return [{"edge_id": r["id"], "type": r["type"], "newer_fact": f(r["newer"]),
             "older_fact": f(r["older"]), "verdict": "", "notes": ""} for r in rows[:n]]


def audit_rows_from_shadow(conn, ws, n, seed, do_redact):
    cur = conn.cursor()
    cur.execute("SELECT id, new_summary, decision, candidates FROM reconcile_shadow_log "
                "WHERE workspace_id=%s AND owner_id IS NULL ORDER BY id DESC LIMIT 5000", (ws,))
    rows = cur.fetchall()
    cur.close()
    f = redact if do_redact else (lambda x: x)
    out = []
    for r in rows:
        dec = r["decision"] if isinstance(r["decision"], dict) else json.loads(r["decision"])
        cands = r["candidates"] if isinstance(r["candidates"], list) else json.loads(r["candidates"])
        by_id = {c["id"]: c for c in cands}
        for kind, ids in (("would_supersede", dec.get("supersedes", [])),
                          ("would_conflict", dec.get("in_tension", []))):
            for cid in ids:
                c = by_id.get(str(cid))
                if c:
                    out.append({"edge_id": f"shadow-{r['id']}", "type": kind,
                                "newer_fact": f(r["new_summary"]), "older_fact": f(c["summary"]),
                                "verdict": "", "notes": ""})
    random.Random(seed).shuffle(out)
    return out[:n]


def score_audit(rows):
    """Precision of reviewed supersession decisions, with a 95% interval."""
    def tally(subset):
        correct = sum(1 for r in subset if r["verdict"].strip().lower() == "correct")
        wrong = sum(1 for r in subset if r["verdict"].strip().lower() == "wrong")
        unsure = sum(1 for r in subset if r["verdict"].strip().lower() == "unsure")
        return {"precision": prop(correct, correct + wrong), "correct": correct,
                "wrong": wrong, "unsure": unsure,
                "unreviewed": sum(1 for r in subset if not r["verdict"].strip())}
    out = {"overall": tally(rows)}
    for t in sorted({r["type"] for r in rows}):
        out[t] = tally([r for r in rows if r["type"] == t])
    return out


# ── command line ─────────────────────────────────────────────────────────────

def _md_prop(p):
    if not p or not p.get("n"):
        return "n/a"
    lo, hi = p["ci95"]
    return f"{p['value']:.3f} ({p['k']}/{p['n']}, 95% CI {lo:.2f}-{hi:.2f})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="export real questions for labelling")
    s.add_argument("--ws", required=True)
    s.add_argument("--team")
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--redact", action="store_true")
    s.add_argument("--out", default="labels.yaml")

    c = sub.add_parser("score", help="score a labelled set")
    c.add_argument("--ws", required=True)
    c.add_argument("--team", required=True)
    c.add_argument("--labels", required=True)
    c.add_argument("--caller", default=os.getenv("BRAIN_USER", "evaluator"))
    c.add_argument("--principals", default="everyone",
                   help="comma-separated; must be able to see the gold facts")
    c.add_argument("--answers", action="store_true", help="also judge faithfulness and citations (uses a model)")
    c.add_argument("--out", default="real_results.json")

    a = sub.add_parser("audit-sample", help="sample supersession decisions for review")
    a.add_argument("--ws", required=True)
    a.add_argument("--n", type=int, default=100)
    a.add_argument("--seed", type=int, default=7)
    a.add_argument("--source", choices=("edges", "shadow"), default="edges")
    a.add_argument("--redact", action="store_true")
    a.add_argument("--out", default="audit.csv")

    b = sub.add_parser("audit-score", help="precision from a reviewed audit sheet")
    b.add_argument("--file", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "audit-score":
        with open(args.file, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        res = score_audit(rows)
        print(json.dumps(res, indent=2))
        o = res["overall"]
        p = o["precision"]
        if p["n"]:
            verdict = "meets" if p["ci95"][0] >= PRECISION_FLOOR else "does not yet demonstrate"
            print(f"\nSupersession precision {_md_prop(p)} — {verdict} the {PRECISION_FLOOR:.2f} floor "
                  f"(judged on the lower confidence bound). Unreviewed rows: {o['unreviewed']}.")
        return 0

    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    from dotenv import load_dotenv
    load_dotenv()
    conn = _connect(args.ws)

    if args.cmd == "sample":
        entries = sample_questions(conn, args.ws, args.team, args.n, args.seed, args.redact)
        header = ("# Label each question. For answerable ones, list the fact ids that SHOULD answer it\n"
                  "# under `gold:` (ids are shown in `returned_then`, or look others up). If the memory\n"
                  "# cannot answer it, set `answerable: false`. Include about 15% unanswerable.\n")
        Path(args.out).write_text(header + yaml.safe_dump(entries, sort_keys=False, allow_unicode=True),
                                  encoding="utf-8")
        print(f"wrote {len(entries)} questions to {args.out}")
        return 0

    if args.cmd == "score":
        from brain.engine import Brain
        entries = yaml.safe_load(Path(args.labels).read_text(encoding="utf-8")) or []
        brain = Brain(workspace_id=args.ws, team_id=args.team, caller=args.caller,
                      principals=[p.strip() for p in args.principals.split(",") if p.strip()])
        res = score_labelled(entries, lambda q: brain.search(q, surface="eval"),
                             with_answers=args.answers)
        Path(args.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
        r = res["retrieval"]
        print(f"labelled answerable: {res['n_labelled_answerable']}  "
              f"unanswerable: {res['n_unanswerable']}  skipped (unlabelled): {res['n_skipped_unlabelled']}")
        print(f"hit@{res['k']}: {_md_prop(r['hit@k'])}   recall: {r['recall@k']}   mrr: {r['mrr']}   ndcg: {r['ndcg@k']}")
        print(f"abstention on unanswerable: {_md_prop(res['abstention'])}")
        if "answers" in res:
            for k_, v in res["answers"].items():
                print(f"{k_}: {_md_prop(v)}")
        lo = r["hit@k"]["ci95"][0] if r["hit@k"]["n"] else None
        if lo is not None:
            print(f"\nretrieval {'meets' if lo >= RETRIEVAL_FLOOR else 'does not yet demonstrate'} the "
                  f"{RETRIEVAL_FLOOR:.2f} floor (lower confidence bound {lo:.2f}).")
        return 0

    if args.cmd == "audit-sample":
        fn = audit_rows_from_shadow if args.source == "shadow" else audit_rows_from_edges
        rows = fn(conn, args.ws, args.n, args.seed, args.redact)
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=AUDIT_COLUMNS)
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {len(rows)} decisions to {args.out}; fill `verdict` with correct / wrong / unsure")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
