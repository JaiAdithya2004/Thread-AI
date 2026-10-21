"""Thread benchmark: retrieval, answer quality, and fact supersession.

    python -m evals.bench                      # everything
    python -m evals.bench --parts A,B          # a subset
    python -m evals.bench --parts A --no-answers   # retrieval only, no LLM calls

Uses the synthetic corpus in evals/corpus.yaml (fictional company, no personal
data) and explicit ground truth that does not depend on what retrieval returns.

  Part A  Retrieval and answer quality on a static knowledge base.
          Retrieval is scored for vector-only, full-text-only and hybrid (RRF)
          rankings, plus the production path. Answers are scored for
          correctness, faithfulness, citation support and abstention.

  Part B  Supersession decisions, pair by pair. Each new statement is judged
          against a database in its true state, by three methods: the previous
          exact-subject approach, embedding similarity alone, and the current
          embedding + LLM-judge reconcile step.

  Part C  The same facts fed through the real save pipeline in sequence, then
          queried: is the stored state right, and does "what is the current X?"
          return the latest value? Run once with reconciliation and once with
          it disabled, which reproduces the earlier behaviour.

Runs against a local database only unless --allow-remote is given: it deletes
and recreates the ws_bench_* workspaces.
"""
import argparse
import datetime
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import unittest.mock as mock
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yaml

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CORPUS_PATH = Path(__file__).with_name("corpus.yaml")
TEAM = "bench"
K = 5
SUFFIX = os.getenv("BENCH_WS_SUFFIX", "")   # lets several runs share one database
JUDGE_MODEL = os.getenv("BENCH_JUDGE_MODEL", "openrouter/openai/gpt-4o-mini")
NS = uuid.UUID("6f1d3c52-8a4e-4b7a-9d2e-2b0c7a9e1f10")

# Phrases that mean "the memory has no answer".
_ABSTAIN_RE = re.compile(
    r"(don't|do not|doesn't|does not|no)\s+(have|contain|include|specify|mention|know)"
    r"|not\s+(specified|mentioned|provided|available|stated|found)"
    r"|(can't|cannot|unable to)\s+(help|find|answer|determine)"
    r"|no (relevant )?(information|facts?)",
    re.I,
)


# ── small statistics helpers ──────────────────────────────────────────────────

def wilson(k: int, n: int, z: float = 1.96):
    """95% Wilson score interval for a proportion."""
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - h), 3), round(min(1.0, c + h), 3))


def prop(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "value": round(k / n, 4) if n else None, "ci95": [lo, hi]}


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    i = min(len(xs) - 1, max(0, math.ceil(p * len(xs)) - 1))
    return round(xs[i], 3)


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ── ranking metrics against explicit gold ─────────────────────────────────────

def rank_metrics(ranked: list, gold: list, k: int = K) -> dict:
    top = ranked[:k]
    g = set(gold)
    hits = [x for x in top if x in g]
    first = next((i for i, x in enumerate(ranked, 1) if x in g), None)
    dcg = sum(1 / math.log2(i + 1) for i, x in enumerate(top, 1) if x in g)
    idcg = sum(1 / math.log2(i + 1) for i in range(1, min(len(g), k) + 1))
    return {
        "hit": float(bool(hits)),
        "recall": len(set(hits)) / len(g),
        "precision": len(hits) / k,
        "mrr": 1 / first if first else 0.0,
        "ndcg": dcg / idcg if idcg else 0.0,
    }


# ── workspace plumbing ────────────────────────────────────────────────────────

def fid(ws: str, key: str) -> str:
    return str(uuid.uuid5(NS, f"{ws}:{key}"))


def reset_ws(conn, ws: str):
    cur = conn.cursor()
    cur.execute("DELETE FROM fact_edges WHERE workspace_id=%s", (ws,))
    cur.execute("DELETE FROM facts WHERE workspace_id=%s", (ws,))
    conn.commit()
    cur.close()


class Clock:
    """Strictly increasing timestamps so history order is deterministic."""

    def __init__(self):
        self.t = datetime.datetime.utcnow() - datetime.timedelta(days=1)

    def next(self) -> str:
        self.t += datetime.timedelta(seconds=1)
        return self.t.isoformat()


def put_fact(conn, ws, key, subject, summary, emb, clock, invalid_at=None):
    from brain.core import db
    db.save_fact(conn, ws, {
        "id": fid(ws, key), "subject": subject, "predicate": "states", "object": "",
        "summary": summary, "episode_id": None, "acl": "everyone", "confidence": 0.9,
        "introduced_at": clock.next(), "invalidated_at": invalid_at,
        "source_kind": "llm", "team_id": TEAM, "visibility": "team",
    }, emb)


def git_rev() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=Path(__file__).parent.parent,
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


# ── LLM judge (independent model family from the answerer) ───────────────────

_FAITH_SYS = ("You are a strict evaluator. Decide whether EVERY claim in the answer is "
              "supported by the facts given. Reply JSON only: "
              '{"faithful": true|false, "unsupported": ["..."]}')
_CITE_SYS = ("You are a strict evaluator. Decide whether the cited fact supports the "
             'sentence. Reply JSON only: {"supported": true|false}')


def judge_faithful(question, answer, facts):
    from brain.core import llm
    block = "\n".join(f"- {f['summary']}" for f in facts)
    r = llm.chat_json(_FAITH_SYS,
                      f"Question: {question}\n\nAnswer: {answer}\n\nFacts:\n{block}",
                      model=JUDGE_MODEL, task_name="bench_judge")
    return r.get("faithful") if isinstance(r, dict) and "faithful" in r else None


def judge_supported(sentence, fact_summary):
    from brain.core import llm
    r = llm.chat_json(_CITE_SYS, f'Sentence: "{sentence}"\nCited fact: "{fact_summary}"',
                      model=JUDGE_MODEL, task_name="bench_judge")
    return r.get("supported") if isinstance(r, dict) and "supported" in r else None


def citation_scores(answer: str, facts: list) -> dict:
    by_id = {f["fact_id"]: f["summary"] for f in facts}
    total = valid = supported = judged = 0
    for sentence in re.split(r"(?<=[.!?])\s+", answer):
        for ref in re.findall(r"\[([0-9a-fA-F\-]{8,})\]", sentence):
            total += 1
            if ref in by_id:
                valid += 1
                s = judge_supported(sentence, by_id[ref])
                if s is not None:
                    judged += 1
                    supported += bool(s)
    return {"total": total, "valid": valid, "judged": judged, "supported": supported}


def contains_ok(answer: str, q: dict) -> bool:
    a = answer.lower()
    if q.get("contains_all"):
        return all(x.lower() in a for x in q["contains_all"])
    return any(x.lower() in a for x in q.get("contains_any", []))


# ── Part A ────────────────────────────────────────────────────────────────────

def part_a(corpus, conn, answers: bool) -> dict:
    from brain import config
    from brain.core import embed, retrieve
    from brain.engine import Brain

    ws = f"ws_bench_a{SUFFIX}"
    reset_ws(conn, ws)
    facts = corpus["facts"]
    embs = embed.embed_batch([f["summary"] for f in facts])
    clock = Clock()
    for f, e in zip(facts, embs):
        put_fact(conn, ws, f["key"], f["subject"], f["summary"], e, clock)
    key_of = {fid(ws, f["key"]): f["key"] for f in facts}
    gold_ids = {k: v for v, k in key_of.items()}

    rankers = {"vector": [], "fulltext": [], "hybrid_rrf": [], "production": []}
    per_q, lat = {m: [] for m in rankers}, []
    rows = []
    for q in corpus["questions"]:
        qe = embed.embed(q["q"])
        vec = retrieve._vec(conn, qe, 30, workspace_id=ws)
        fts = [i for i in retrieve._fts(conn, q["q"], 30) if i in key_of]
        rrf = retrieve._rrf(vec, fts)
        t0 = time.perf_counter()
        prod = [f["id"] for f in retrieve.run(conn, ws, q["q"], config.CALLER_PRINCIPALS,
                                              config.CALLER, "eval", team_id=TEAM)]
        lat.append(time.perf_counter() - t0)
        for name, ranked in (("vector", vec), ("fulltext", fts),
                             ("hybrid_rrf", rrf), ("production", prod)):
            m = rank_metrics([key_of.get(i, i) for i in ranked], q["gold"])
            per_q[name].append(m)
        rows.append({"id": q["id"], "gold": q["gold"],
                     "production_top": [key_of.get(i, i) for i in prod[:K]]})

    retrieval = {}
    for name, ms in per_q.items():
        retrieval[name] = {
            "hit@5": prop(int(sum(m["hit"] for m in ms)), len(ms)),
            "recall@5": mean([m["recall"] for m in ms]),
            "precision@5": mean([m["precision"] for m in ms]),
            "mrr": mean([m["mrr"] for m in ms]),
            "ndcg@5": mean([m["ndcg"] for m in ms]),
        }
    out = {"corpus_facts": len(facts), "n_answerable": len(corpus["questions"]),
           "k": K, "retrieval": retrieval,
           "retrieval_latency_s": {"p50": pct(lat, .5), "p95": pct(lat, .95), "mean": mean(lat)},
           "rerank_enabled": config.RERANK_ENABLED, "per_question": rows}
    if not answers:
        return out

    brain = Brain(workspace_id=ws, team_id=TEAM)
    ans_rows, a_lat = [], []
    correct = faithful_k = faithful_n = no_syn = 0
    cite = {"total": 0, "valid": 0, "judged": 0, "supported": 0}
    for q in corpus["questions"]:
        t0 = time.perf_counter()
        r = brain.search(q["q"], surface="eval")
        a_lat.append(time.perf_counter() - t0)
        text = (r.get("synthesized_answer") or "").strip()
        fl = r.get("facts", [])
        if not text:
            no_syn += 1
            ans_rows.append({"id": q["id"], "answer": "", "note": "no synthesis"})
            continue
        ok = contains_ok(text, q)
        correct += ok
        f = judge_faithful(q["q"], text, fl)
        if f is not None:
            faithful_n += 1
            faithful_k += bool(f)
        c = citation_scores(text, fl)
        for kk in cite:
            cite[kk] += c[kk]
        ans_rows.append({"id": q["id"], "answer": text[:300], "correct": ok,
                         "faithful": f, "citations": c})
    n_ans = len(corpus["questions"]) - no_syn
    abst_k = abst_n = 0
    una_rows = []
    for q in corpus["unanswerable"]:
        r = brain.search(q["q"], surface="eval")
        text = (r.get("synthesized_answer") or "").strip()
        if not text:
            una_rows.append({"id": q["id"], "answer": "", "note": "no synthesis"})
            continue
        abst = bool(_ABSTAIN_RE.search(text)) or "can't help with that" in text.lower()
        abst_n += 1
        abst_k += abst
        una_rows.append({"id": q["id"], "answer": text[:200], "abstained": abst})
    out["answers"] = {
        "answer_correct": prop(correct, n_ans),
        "faithfulness": prop(faithful_k, faithful_n),
        "citation_validity": prop(cite["valid"], cite["total"]),
        "citation_support": prop(cite["supported"], cite["judged"]),
        "abstention": prop(abst_k, abst_n),
        "no_synthesis": no_syn,
        "latency_s": {"p50": pct(a_lat, .5), "p95": pct(a_lat, .95), "mean": mean(a_lat)},
        "judge_model": JUDGE_MODEL, "answers": ans_rows, "unanswerable": una_rows,
    }
    return out


# ── Part B ────────────────────────────────────────────────────────────────────

def _decision_legacy(item, new_subject, new_emb, state):
    """Previous behaviour: act only on an exact subject match.

    Credit is given generously: the extraction model's `supersedes` hint is
    assumed present exactly when the item really is an update.
    """
    same = [(k, s) for k, s in state.items() if s["valid"] and s["subject"] == new_subject]
    if not same:
        return None, set()
    best_k, best = max(same, key=lambda kv: cosine(new_emb, kv[1]["emb"]))
    if cosine(new_emb, best["emb"]) >= 0.92:
        return best_k, set()                      # near-duplicate: dropped
    if item["kind"] == "update":
        latest = max(same, key=lambda kv: kv[1]["order"])[0]
        return None, {latest}
    return None, set()


def _decision_embedding(new_emb, state):
    """Embedding similarity alone: >=0.92 duplicate, 0.75-0.92 supersedes."""
    cands = sorted(((cosine(new_emb, s["emb"]), k) for k, s in state.items() if s["valid"]),
                   reverse=True)
    if not cands or cands[0][0] < 0.75:
        return None, set()
    sim, k = cands[0]
    return (k, set()) if sim >= 0.92 else (None, {k})


def part_b(corpus, conn) -> dict:
    from brain import config
    from brain.core import embed, reconcile

    ws = f"ws_bench_b{SUFFIX}"
    reset_ws(conn, ws)
    clock = Clock()
    state = {}     # key -> {subject, emb, valid, order}
    order = 0

    base = corpus["facts"]
    chain_first = [(f"{c['id']}.v0", c["versions"][0]) for c in corpus["chains"]]
    items = []
    for c in corpus["chains"]:
        for i, v in enumerate(c["versions"][1:], 1):
            items.append({"id": f"{c['id']}.v{i}", "kind": "update", "text": v["text"],
                          "subject": v["subject"], "gold_sup": f"{c['id']}.v{i-1}", "gold_dup": None})
    for d in corpus["decoys"]:
        items.append({"id": d["id"], "kind": "decoy", "text": d["text"], "subject": d["subject"],
                      "gold_sup": None, "gold_dup": None})
    for f in corpus["futures"]:
        items.append({"id": f["id"], "kind": "future", "text": f["text"], "subject": f["subject"],
                      "gold_sup": None, "gold_dup": None})
    for r in corpus["rewordings"]:
        items.append({"id": r["id"], "kind": "reword", "text": r["text"], "subject": r["subject"],
                      "gold_sup": None, "gold_dup": r["target"]})

    texts = ([f["summary"] for f in base] + [v["text"] for _, v in chain_first]
             + [it["text"] for it in items])
    embs = embed.embed_batch(texts)
    e_iter = iter(embs)
    for f in base:
        e = next(e_iter)
        put_fact(conn, ws, f["key"], f["subject"], f["summary"], e, clock)
        state[f["key"]] = {"subject": f["subject"], "emb": e, "valid": True, "order": order}
        order += 1
    for key, v in chain_first:
        e = next(e_iter)
        put_fact(conn, ws, key, v["subject"], v["text"], e, clock)
        state[key] = {"subject": v["subject"], "emb": e, "valid": True, "order": order}
        order += 1
    item_embs = {it["id"]: next(e_iter) for it in items}
    key_of = {fid(ws, k): k for k in state}
    chain_vers = {c["id"]: c["versions"] for c in corpus["chains"]}

    methods = ("legacy_exact_subject", "embedding_only", "reconcile")
    res = {m: [] for m in methods}
    cand_hit = cand_n = 0
    for it in items:
        e = item_embs[it["id"]]
        leg = _decision_legacy(it, it["subject"], e, state)
        emb_only = _decision_embedding(e, state)
        dec = reconcile.reconcile(conn, ws, {"summary": it["text"], "subject": it["subject"]},
                                  e, TEAM)
        ours = (key_of.get(dec.duplicate_of), {key_of[x] for x in dec.supersedes})
        if it["kind"] == "update":
            cands = reconcile.find_related(conn, ws, e, TEAM)
            cand_n += 1
            cand_hit += any(key_of.get(c["id"]) == it["gold_sup"] for c in cands)
        for m, (dup, sup) in zip(methods, (leg, emb_only, ours)):
            res[m].append({"item": it["id"], "kind": it["kind"], "dup": dup, "sup": sorted(sup),
                           "gold_sup": it["gold_sup"], "gold_dup": it["gold_dup"]})
        # advance the true state: apply the gold action for updates only
        if it["kind"] == "update":
            cid, vi = it["id"].split(".v")
            vi = int(vi)
            pred = it["gold_sup"]
            from brain.core import db
            db.invalidate_fact(conn, ws, fid(ws, pred), clock.next())
            state[pred]["valid"] = False
            put_fact(conn, ws, it["id"], it["subject"], it["text"], e, clock)
            state[it["id"]] = {"subject": it["subject"], "emb": e, "valid": True, "order": order}
            key_of[fid(ws, it["id"])] = it["id"]
            order += 1

    summary = {}
    for m in methods:
        rows = res[m]
        tp = fp = fn = lost = 0
        for r in rows:
            sup = set(r["sup"])
            if r["kind"] == "update":
                if r["gold_sup"] in sup:
                    tp += 1
                    fp += len(sup - {r["gold_sup"]})
                else:
                    fn += 1
                    fp += len(sup)
                    lost += r["dup"] is not None
            else:
                fp += len(sup)
        upd = [r for r in rows if r["kind"] == "update"]
        rew = [r for r in rows if r["kind"] == "reword"]
        dcy = [r for r in rows if r["kind"] == "decoy"]
        fut = [r for r in rows if r["kind"] == "future"]
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else 0.0
        summary[m] = {
            "supersession_precision": round(prec, 4) if prec is not None else None,
            "supersession_recall": prop(tp, tp + fn),
            "supersession_f1": round(f1, 4),
            "updates_dropped_as_duplicate": prop(lost, len(upd)),
            "duplicate_detection": prop(sum(r["dup"] == r["gold_dup"] for r in rew), len(rew)),
            "decoy_false_actions": prop(sum(bool(r["sup"]) or r["dup"] is not None for r in dcy), len(dcy)),
            "future_dated_wrongly_superseded": prop(sum(bool(r["sup"]) for r in fut), len(fut)),
            "total_wrong_supersessions": fp,
        }
    return {"items": len(items), "n_updates": sum(i["kind"] == "update" for i in items),
            "candidate_recall_of_true_predecessor": prop(cand_hit, cand_n),
            "related_threshold": config.RELATED_THRESHOLD, "k_candidates": config.RECONCILE_K,
            "methods": summary, "detail": res}


# ── Part C ────────────────────────────────────────────────────────────────────

def _val_re(v: str):
    # An optional trailing "s" so a value like "Thursday" also matches "Thursdays".
    return re.compile(r"(?<![\w$.\-])" + re.escape(v) + r"s?(?![\w])", re.I)


def part_c(corpus, conn) -> dict:
    from brain import config
    from brain.core import embed, extract, reconcile
    from brain.engine import Brain

    base = corpus["facts"]
    base_embs = embed.embed_batch([f["summary"] for f in base])
    chain_texts = [v["text"] for c in corpus["chains"] for v in c["versions"]]
    chain_embs = dict(zip(chain_texts, embed.embed_batch(chain_texts)))
    out = {}
    for mode in ("legacy", "reconcile"):
        ws = f"ws_bench_c_{mode}{SUFFIX}"
        reset_ws(conn, ws)
        clock = Clock()
        for f, e in zip(base, base_embs):
            put_fact(conn, ws, f["key"], f["subject"], f["summary"], e, clock)
        ep = {"id": None, "team_id": TEAM, "acl": "everyone", "source": "bench"}
        patcher = mock.patch.object(reconcile, "judge", return_value=None) if mode == "legacy" \
            else mock.patch.object(reconcile, "judge", wraps=reconcile.judge)
        with patcher:
            for c in corpus["chains"]:
                for v in c["versions"]:
                    fact = {"subject": v["subject"], "predicate": "states", "object": v["value"],
                            "summary": v["text"], "confidence": 0.9, "source_kind": "llm"}
                    if extract._reconcile_into(conn, ws, fact, chain_embs[v["text"]], ep):
                        extract._save(conn, ws, fact, ep, chain_embs[v["text"]])

        cur = conn.cursor()
        cur.execute("SELECT id, summary, invalidated_at FROM facts WHERE workspace_id=%s", (ws,))
        rows = cur.fetchall()
        cur.close()
        by_sum = {r["summary"]: r for r in rows}
        brain = Brain(workspace_id=ws, team_id=TEAM)
        base_collateral = sum(1 for f in base
                              if by_sum[f["summary"]]["invalidated_at"] is not None)

        stored = status_ok = status_n = chains_ok = 0
        top1 = ans_ok = hist_recall_k = hist_status_k = hist_n = 0
        c_rows = []
        for c in corpus["chains"]:
            vs = c["versions"]
            present = [v["text"] in by_sum for v in vs]
            stored += sum(present)
            exp = [False] * (len(vs) - 1) + [True]      # expected "valid" per version
            ok_all = True
            for v, want_valid in zip(vs, exp):
                row = by_sum.get(v["text"])
                status_n += 1
                good = row is not None and (row["invalidated_at"] is None) == want_valid
                status_ok += good
                ok_all &= good
            chains_ok += ok_all

            final = vs[-1]
            r = brain.search(c["current_q"], surface="eval")
            facts = r.get("facts", [])
            top = facts[0]["summary"] if facts else ""
            top1 += top == final["text"]
            text = (r.get("synthesized_answer") or "")
            has_final = bool(_val_re(final["value"]).search(text))
            has_old = any(_val_re(v["value"]).search(text) for v in vs[:-1])
            ans_ok += has_final and not has_old

            h = brain.decision_history(c["history_q"])
            tl = {t["summary"]: t["status"] for t in h["timeline"]}
            for v, want_valid in zip(vs, exp):
                hist_n += 1
                hist_recall_k += v["text"] in tl
                hist_status_k += (v["text"] in tl
                                  and (tl[v["text"]] == "valid") == want_valid)
            c_rows.append({"chain": c["id"], "stored": present, "state_correct": ok_all,
                           "top1_is_final": top == final["text"],
                           "answer_has_final_only": has_final and not has_old,
                           "answer": text[:200]})
        n_ver = sum(len(c["versions"]) for c in corpus["chains"])
        n_ch = len(corpus["chains"])
        out[mode] = {
            "versions_stored": prop(stored, n_ver),
            "version_status_correct": prop(status_ok, status_n),
            "chains_fully_correct": prop(chains_ok, n_ch),
            "current_top1_is_latest": prop(top1, n_ch),
            "current_answer_latest_only": prop(ans_ok, n_ch),
            "history_recall": prop(hist_recall_k, hist_n),
            "history_status_correct": prop(hist_status_k, hist_n),
            "base_facts_wrongly_invalidated": base_collateral,
            "detail": c_rows,
        }
    return out


# ── reporting ─────────────────────────────────────────────────────────────────

def _p(x):
    if isinstance(x, dict) and "value" in x:
        lo, hi = x["ci95"]
        return f"{x['value']:.3f} ({x['k']}/{x['n']}, CI {lo:.2f}-{hi:.2f})" if x["n"] else "n/a"
    return "n/a" if x is None else f"{x:.3f}"


def print_report(res: dict):
    a = res.get("A")
    if a:
        print(f"\n== Part A: retrieval (k={a['k']}, {a['corpus_facts']} facts, "
              f"{a['n_answerable']} questions, rerank={'on' if a['rerank_enabled'] else 'off'}) ==")
        print(f"{'ranker':<12} {'hit@5':<28} recall  prec    mrr     ndcg")
        for name, m in a["retrieval"].items():
            print(f"{name:<12} {_p(m['hit@5']):<28} {m['recall@5']:.3f}   {m['precision@5']:.3f}   "
                  f"{m['mrr']:.3f}   {m['ndcg@5']:.3f}")
        print("retrieval latency:", a["retrieval_latency_s"])
        if "answers" in a:
            x = a["answers"]
            print("\n-- answer quality --")
            for k in ("answer_correct", "faithfulness", "citation_validity",
                      "citation_support", "abstention"):
                print(f"{k:<20} {_p(x[k])}")
            print("no synthesis:", x["no_synthesis"], "| latency:", x["latency_s"])
    b = res.get("B")
    if b:
        print(f"\n== Part B: supersession ({b['n_updates']} updates, {b['items']} items) ==")
        print("true predecessor in reconcile candidates:", _p(b["candidate_recall_of_true_predecessor"]))
        for m, s in b["methods"].items():
            print(f"\n[{m}]")
            for k, v in s.items():
                print(f"  {k:<34} {_p(v)}")
    c = res.get("C")
    if c:
        print("\n== Part C: end-to-end through the save pipeline ==")
        keys = list(c["reconcile"].keys())[:-2]
        print(f"{'metric':<34} {'legacy':<30} reconcile")
        for k in keys:
            print(f"{k:<34} {_p(c['legacy'][k]):<30} {_p(c['reconcile'][k])}")
        print(f"{'base_facts_wrongly_invalidated':<34} {c['legacy']['base_facts_wrongly_invalidated']:<30} "
              f"{c['reconcile']['base_facts_wrongly_invalidated']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default="A,B,C")
    ap.add_argument("--no-answers", action="store_true", help="Part A retrieval only (no LLM calls)")
    ap.add_argument("--out", default=str(Path(__file__).with_name("bench_results.json")))
    ap.add_argument("--allow-remote", action="store_true")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()
    url = os.getenv("DATABASE_URL", "")
    host = urlparse(url).hostname or ""
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    if host not in ("localhost", "127.0.0.1", "::1") and not args.allow_remote:
        print(f"Refusing to run: the benchmark deletes ws_bench_* data and DATABASE_URL points at "
              f"{host!r}. Use a local database, or pass --allow-remote.", file=sys.stderr)
        return 1

    corpus = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))
    from brain import config
    from brain.core import db, embed

    # A failed embedding becomes a zero vector, which silently makes every
    # similarity score meaningless. Abort rather than publish numbers measured
    # on broken embeddings.
    _orig_embed_texts = embed.embed_texts

    def _checked_embed_texts(texts, task_type="RETRIEVAL_DOCUMENT"):
        vecs = _orig_embed_texts(texts, task_type)
        if any(not any(v) for v in vecs):
            raise RuntimeError("embedding failed (zero vector returned); results would be invalid")
        return vecs

    embed.embed_texts = _checked_embed_texts
    conn = db.connect()
    db.migrate(conn)

    parts = [p.strip().upper() for p in args.parts.split(",") if p.strip()]
    res = {"meta": {
        "date": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "git": git_rev(), "embed_model": config.EMBED_MODEL, "embed_dim": config.EMBED_DIM,
        "extract_model": config.EXTRACT_MODEL, "synth_model": config.SYNTH_MODEL,
        "judge_model": JUDGE_MODEL, "related_threshold": config.RELATED_THRESHOLD,
        "dedup_threshold": config.DEDUP_THRESHOLD, "k_candidates": config.RECONCILE_K,
        "rerank_enabled": config.RERANK_ENABLED,
    }}
    if "A" in parts:
        res["A"] = part_a(corpus, conn, answers=not args.no_answers)
    if "B" in parts:
        res["B"] = part_b(corpus, conn)
    if "C" in parts:
        res["C"] = part_c(corpus, conn)
    Path(args.out).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print_report(res)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
