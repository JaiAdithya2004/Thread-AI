"""End-to-end benchmark: raw messages in, answers out.

    python -m evals.e2e synthetic                 # labelled raw-message corpus (evals/e2e_corpus.yaml)
    python -m evals.e2e real --owner COMPOSIO_USER_ID --days 90    # your own Gmail via Composio
    python -m evals.e2e aggregate run1.json run2.json run3.json    # pool several runs

Where evals/bench.py hands the system finished facts, this feeds it what connectors
deliver and exercises the WHOLE pipeline with the real models and embeddings:

    ingest (noise filter, injection scrub, dedupe) -> extract (LLM) -> reconcile
    (embeddings + judge) -> retrieve (hybrid) -> answer (LLM) -> judge

Every stage is scored, so a failure is attributed to the stage that caused it.

Synthetic track  Ground truth is written into the corpus: which claims each message
                 contains, which later message replaces which, what is noise, what is
                 a decoy, what tries to hijack the model. Fully labelled and repeatable.
Real track       Your own mail. There are no labels, so it measures what can be
                 measured without them: whether each extracted fact is actually
                 supported by its source email (judge), and whether questions
                 generated from those facts retrieve and answer correctly. Raw text
                 stays in evals/results/private/ (git-ignored); only aggregates are
                 written to the public results file.

Runs against a local database only unless --allow-remote is given.
"""
import argparse
import contextlib
import csv
import datetime
import json
import math
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

from evals.bench import (K, _ABSTAIN_RE, JUDGE_MODEL, citation_scores, embedding_guard,
                         git_rev, judge_faithful, mean, pct, prop, rank_metrics, wilson)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).parent
CORPUS = HERE / "e2e_corpus.yaml"
TEAM = "e2e"


# ── small helpers ────────────────────────────────────────────────────────────

def tok_re(tok: str):
    """Case-insensitive, word-bounded token match ('4' must not match '24' or '4.2')."""
    return re.compile(r"(?<![\w.])" + re.escape(tok) + r"s?(?![\w]|\.\d)", re.I)


def has_all(tokens, text) -> bool:
    return all(tok_re(t).search(text or "") for t in tokens)


def has_any(tokens, text) -> bool:
    return any(tok_re(t).search(text or "") for t in tokens)


def _openrouter_usage():
    """Total spend reported by the provider, to measure the cost of a run. None if unknown."""
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        return None
    try:
        import requests
        r = requests.get("https://openrouter.ai/api/v1/credits",
                         headers={"Authorization": f"Bearer {key}"}, timeout=15)
        return float(r.json()["data"]["total_usage"])
    except Exception:
        return None


def _judge_json(system, user):
    from brain.core import llm
    r = llm.chat_json(system, user, model=JUDGE_MODEL, task_name="bench_judge")
    return r if isinstance(r, dict) else {}


_SUPPORT_SYS = ("You check a memory system's extraction. Decide whether the FACT is stated in, or "
                "directly follows from, the SOURCE message. Be strict: a detail that is not in the "
                'source is unsupported. Reply JSON only: {"supported": true|false}')


def judge_supported(source: str, fact: str):
    r = _judge_json(_SUPPORT_SYS, f"SOURCE:\n{source[:4000]}\n\nFACT:\n{fact}")
    return bool(r["supported"]) if "supported" in r else None


def preflight() -> list:
    """Prove the three models can serve a full-size request BEFORE spending a run.

    Providers reserve credit for a request's whole output ceiling up front, so a
    nearly empty account passes a one-word test and then refuses every real call.
    Probing each model at the ceiling the pipeline actually uses finds that in
    seconds. Returns a list of problems (empty when everything works).
    """
    from brain import config
    from brain.core import llm
    problems = []
    probes = [
        ("extraction model", config.EXTRACT_MODEL,
         lambda m: llm.chat_json("Reply JSON only.", 'Return {"ok": true}', model=m, task_name="extract")),
        ("answer model", config.SYNTH_MODEL,
         lambda m: llm.chat_text("Reply with one word.", "Say ok.", model=m, task_name="synthesize")),
        ("judge model", JUDGE_MODEL,
         lambda m: llm.chat_json("Reply JSON only.", 'Return {"ok": true}', model=m, task_name="bench_judge")),
    ]
    for label, model, call in probes:
        # One retry: a single rate-limit or network blip must not abort a long run,
        # while a genuinely refusing provider (no credit) fails both attempts.
        for attempt in (1, 2):
            out = call(model)
            if out and not (isinstance(out, dict) and "ok" not in out):
                break
            if attempt == 1:
                time.sleep(4)
        else:
            problems.append(f"{label} ({model}) could not serve a full-size request")
    return problems


def reset_workspace(conn, ws):
    cur = conn.cursor()
    for sql in ("DELETE FROM fact_edges WHERE workspace_id=%s", "DELETE FROM facts WHERE workspace_id=%s",
                "DELETE FROM episodes WHERE workspace_id=%s", "DELETE FROM ledger WHERE workspace_id=%s"):
        cur.execute(sql, (ws,))
    conn.commit()
    cur.close()


def _require_local(allow_remote: bool):
    url = os.getenv("DATABASE_URL", "")
    host = urlparse(url).hostname or ""
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    if host not in ("localhost", "127.0.0.1", "::1") and not allow_remote:
        raise SystemExit(f"Refusing to run: this deletes the ws_e2e_* workspaces and DATABASE_URL points at "
                         f"{host!r}. Use a local database or pass --allow-remote.")


def _unextracted(conn, ws) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM episodes WHERE workspace_id=%s AND extracted=0", (ws,))
    n = cur.fetchone()["n"]
    cur.close()
    return n


def _drain_extraction(conn, ws, max_rounds=12, patience=2):
    """Run extraction until nothing is left unextracted. Returns (facts_created, rounds).

    Episodes whose extraction fails are left unextracted so a later pass retries
    them. Stop after `patience` rounds that make no progress: a provider that is
    refusing requests will not recover by being asked forty times.
    """
    from brain.core import extract
    total = rounds = stalled = 0
    left = _unextracted(conn, ws)
    while left and rounds < max_rounds and stalled < patience:
        rounds += 1
        total += extract.run(conn, ws, limit=20)
        now = _unextracted(conn, ws)
        stalled = stalled + 1 if now >= left else 0
        left = now
    return total, rounds


def _health(unextracted: int, answerable_asked: int, answerable_blank: int, judge_failures: int = 0) -> dict:
    """Is each stage a measurement of the system, or of a provider outage?

    A refused or failed model call silently becomes an empty extraction or an empty
    answer, which then scores as a quality failure. So validity is judged per stage:
    extraction, state and retrieval need only the extraction model and embeddings;
    answers also need the answer model. A run whose answers failed still yields
    honest extraction, state and retrieval numbers, and says exactly which sections
    to ignore, so a billing problem can never be mistaken for a quality regression.
    """
    problems = []
    extraction_ok = not unextracted and not judge_failures
    answers_ok = not answerable_blank
    if unextracted:
        problems.append(f"{unextracted} episode(s) could not be extracted (model calls failed)")
    if answerable_blank:
        problems.append(f"{answerable_blank} of {answerable_asked} questions got no answer (model calls failed)")
    if judge_failures:
        problems.append(f"{judge_failures} judge call(s) returned nothing")
    return {"valid": extraction_ok and answers_ok, "problems": problems,
            "stages": {"ingestion": True, "extraction": extraction_ok, "state": extraction_ok,
                       "retrieval": extraction_ok, "answers": extraction_ok and answers_ok},
            "unextracted_episodes": unextracted, "blank_answers": answerable_blank}


# which stage each metrics section depends on (sections absent here are always usable)
SECTION_STAGE = {"ingestion": "ingestion", "extraction": "extraction", "state": "state",
                 "retrieval": "retrieval", "answers": "answers", "reconcile": "extraction"}


# ── synthetic track ──────────────────────────────────────────────────────────

def run_synthetic(args) -> dict:
    from brain import config
    from brain.core import db, db_pg, ingest
    from brain.engine import Brain

    corpus = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    episodes, questions = corpus["episodes"], corpus["questions"]
    ws = f"ws_e2e_syn{os.getenv('BENCH_WS_SUFFIX', '')}"
    conn = db.connect()
    db.migrate(conn)
    db_pg._set_ws(conn, ws)
    reuse = getattr(args, "reuse", False)       # re-score the questions against facts already stored
    if not reuse:
        reset_workspace(conn, ws)
    db.ensure_team(conn, ws, TEAM, "E2E")

    usage0 = _openrouter_usage()
    t_start = time.perf_counter()
    with embedding_guard():
        # 1. ingest, in time order, one message at a time like a live sync would
        by_id = {e["id"]: e for e in episodes}
        t0 = time.perf_counter()
        for e in ([] if reuse else sorted(episodes, key=lambda x: x["date"])):
            ingest.run(conn, ws, [{
                "source": e["source"], "external_id": f"e2e:{e['id']}", "title": e["title"],
                "body": e["body"], "author": e["author"], "url": "", "occurred_at": e["date"],
                "acl": ["everyone"], "team_id": TEAM,
            }], team_id=TEAM, trigger="backfill")
        t_ingest = time.perf_counter() - t0

        # 2. extract (real model) + reconcile (real embeddings and judge)
        t0 = time.perf_counter()
        n_facts, rounds = (0, 0) if reuse else _drain_extraction(conn, ws)
        t_extract = time.perf_counter() - t0
        left_unextracted = _unextracted(conn, ws)

        # ---- read back what the system actually stored
        cur = conn.cursor()
        cur.execute("SELECT id, external_id, body FROM episodes WHERE workspace_id=%s", (ws,))
        stored_eps = {r["external_id"].replace("e2e:", ""): dict(r) for r in cur.fetchall()}
        cur.execute("""SELECT f.id, f.summary, f.subject, f.object, f.invalidated_at, e.external_id
                       FROM facts f LEFT JOIN episodes e ON e.id = f.episode_id
                       WHERE f.workspace_id=%s""", (ws,))
        facts = [dict(r) for r in cur.fetchall()]
        cur.close()
        for f in facts:
            f["ep"] = (f.pop("external_id") or "").replace("e2e:", "")
            f["text"] = " ".join(str(f.get(k) or "") for k in ("summary", "subject", "object"))
            f["stale"] = f["invalidated_at"] is not None

        m = {}

        # ── A. ingestion
        expect = {e["id"]: e.get("expect") for e in episodes}
        dropped = [i for i, x in expect.items() if x == "dropped"]
        dupes = [i for i, x in expect.items() if x == "duplicate"]
        m["ingestion"] = {
            "noise_dropped": prop(sum(i not in stored_eps for i in dropped), len(dropped)),
            "duplicates_dropped": prop(sum(i not in stored_eps for i in dupes), len(dupes)),
            "kept_real_messages": prop(sum(e["id"] in stored_eps for e in episodes if not e.get("expect")),
                                       sum(1 for e in episodes if not e.get("expect"))),
        }
        inj = [e for e in episodes if e.get("injection")]
        m["ingestion"]["injection_phrases_scrubbed"] = prop(
            sum(e["injection"]["phrase"].lower() not in (stored_eps.get(e["id"], {}).get("body") or "").lower()
                for e in inj if e["id"] in stored_eps), len(inj))

        # ── B. extraction
        claims = {c["key"]: dict(c, ep=e["id"]) for e in episodes for c in e.get("claims", [])}
        match = {}
        for key, c in claims.items():
            cand = [f for f in facts if f["ep"] == c["ep"] and has_all(c["all"], f["text"])]
            match[key] = cand
        m["extraction"] = {
            "claim_recall": prop(sum(bool(v) for v in match.values()), len(claims)),
            "facts_extracted": len(facts),
            "facts_per_kept_episode": round(len(facts) / max(1, len(stored_eps)), 2),
            "claims_stored_twice": sum(len(v) > 1 for v in match.values()),
            "false_facts_from_noise": sum(1 for f in facts
                                          if expect.get(f["ep"]) in ("no_facts", "dropped", "duplicate")),
        }
        sup = []
        for f in facts:
            src = stored_eps.get(f["ep"], {}).get("body")
            if src:
                sup.append(judge_supported(src, f["summary"]))
        sup = [s for s in sup if s is not None]
        m["extraction"]["groundedness"] = prop(sum(sup), len(sup))
        m["extraction"]["unsupported_facts"] = len(sup) - sum(sup)

        # ── C. state after the whole timeline
        updated = {c["updates"] for c in claims.values() if c.get("updates")}
        pairs = [(k, c["updates"]) for k, c in claims.items() if c.get("updates")]
        fid = lambda k: match[k][0]["id"] if match.get(k) else None
        stale = {f["id"]: f["stale"] for f in facts}
        # a claim should end up retired exactly when something later replaced it
        want_stale = {k: (k in updated) for k in claims}
        graded = [k for k in claims if match[k]]
        state_ok = [stale[fid(k)] == want_stale[k] for k in graded]
        m["state"] = {
            "claim_state_correct": prop(sum(state_ok), len(state_ok)),
            "update_pairs_correct": prop(
                sum(1 for new, old in pairs if match[new] and match[old]
                    and not stale[fid(new)] and stale[fid(old)]), len(pairs)),
            "false_retirements": sum(1 for k in graded if stale[fid(k)] and not want_stale[k]),
            "missed_retirements": sum(1 for k in graded if not stale[fid(k)] and want_stale[k]),
        }
        decoys = [k for k, c in claims.items() if c.get("decoy_of")]

        def decoy_ok(k):
            """A decoy is handled right when it is stored, left valid, and did not retire its look-alike."""
            t = claims[k]["decoy_of"]
            return bool(match[k]) and not stale[fid(k)] and (not match.get(t) or stale[fid(t)] == want_stale[t])

        m["state"]["decoys_left_alone"] = prop(sum(decoy_ok(k) for k in decoys), len(decoys))
        # chains: every claim in a chain is correct (older ones stale, latest valid)
        chains, seen = [], set()
        for k, c in claims.items():
            if c.get("updates") and k not in updated:           # latest of a chain
                chain, cur_k = [k], k
                while claims[cur_k].get("updates"):
                    cur_k = claims[cur_k]["updates"]
                    chain.append(cur_k)
                chains.append(chain)
        def chain_ok(ch):
            if not all(match[k] for k in ch):
                return False
            return (not stale[fid(ch[0])]) and all(stale[fid(k)] for k in ch[1:])
        m["state"]["chains_fully_correct"] = prop(sum(chain_ok(c) for c in chains), len(chains))

        # ── D/E. retrieval and answers
        brain = Brain(workspace_id=ws, team_id=TEAM)
        rows, lat = [], []
        ranks = {"current": [], "direct": [], "history": []}
        by_kind = {}
        faith_k = faith_n = 0
        cites = {"total": 0, "valid": 0, "judged": 0, "supported": 0}
        inj_ok = inj_n = abst_k = abst_n = latest_k = latest_n = top1_k = top1_n = 0
        asked = blank = 0
        for q in questions:
            t0 = time.perf_counter()
            res = brain.search(q["q"], surface="eval")
            lat.append(time.perf_counter() - t0)
            text = (res.get("synthesized_answer") or "").strip()
            rf = res.get("facts", [])
            kind = q["kind"]
            row = {"id": q["id"], "kind": kind, "answer": text[:300]}
            if kind in ("current", "direct", "history") and rf:
                asked += 1
                blank += not text
            if kind in ("current", "direct", "history"):
                gold_ids = [fid(g) for g in q["gold"]]
                retrievable = all(gold_ids)
                row["retrievable"] = retrievable
                if retrievable:
                    ranks[kind].append(rank_metrics([f["fact_id"] for f in rf], gold_ids))
                    if kind == "current":
                        top1_n += 1
                        top1_k += bool(rf) and rf[0]["fact_id"] == gold_ids[0]
                ok = bool(text) and (has_all(q["contains_all"], text) if q.get("contains_all")
                                     else has_any(q["contains_any"], text))
                row["correct"] = ok
                by_kind.setdefault(kind, []).append(ok)
                if kind == "current":
                    latest_n += 1
                    latest_k += ok and not has_any(q.get("not_contains", []), text)
                if text:
                    fth = judge_faithful(q["q"], text, rf)
                    if fth is not None:
                        faith_n += 1
                        faith_k += bool(fth)
                    c = citation_scores(text, rf)
                    for kk in cites:
                        cites[kk] += c[kk]
            elif kind == "unanswerable":
                refused = (not rf) or bool(_ABSTAIN_RE.search(text)) or "reliable information" in (res.get("answer") or "")
                abst_n += 1
                abst_k += bool(refused)
                row["refused"] = bool(refused)
            elif kind == "injection":
                low = text.lower()
                inj_n += 1
                inj_ok += not any(tok.lower() in low for tok in q["must_not_contain"])
                row["resisted"] = not any(tok.lower() in low for tok in q["must_not_contain"])
            rows.append(row)

        allr = [x for v in ranks.values() for x in v]
        m["retrieval"] = {
            "hit@5_given_extracted": prop(int(sum(x["hit"] for x in allr)), len(allr)),
            "mrr": mean([x["mrr"] for x in allr]),
            "ndcg@5": mean([x["ndcg"] for x in allr]),
            "current_top1_is_latest": prop(top1_k, top1_n),
        }
        m["answers"] = {
            "end_to_end_correct": {k: prop(sum(v), len(v)) for k, v in by_kind.items()},
            "end_to_end_correct_all": prop(sum(sum(v) for v in by_kind.values()),
                                           sum(len(v) for v in by_kind.values())),
            "current_correct_and_concise": prop(latest_k, latest_n),
            "faithfulness": prop(faith_k, faith_n),
            "citation_validity": prop(cites["valid"], cites["total"]),
            "citation_support": prop(cites["supported"], cites["judged"]),
            "abstention": prop(abst_k, abst_n),
            "injection_resisted": prop(inj_ok, inj_n),
        }
        t_total = time.perf_counter() - t_start
        usage1 = _openrouter_usage()
        m["health"] = _health(left_unextracted, asked, blank, judge_failures=(len(facts) - len(sup)))
        m["ops"] = {
            "episodes": len(episodes), "episodes_kept": len(stored_eps),
            "ingest_seconds": round(t_ingest, 1),
            "extract_seconds": round(t_extract, 1), "extract_rounds": rounds,
            "seconds_per_kept_episode_extract": round(t_extract / max(1, len(stored_eps)), 2),
            "search_latency_s": {"p50": pct(lat, .5), "p95": pct(lat, .95)},
            "total_seconds": round(t_total, 1),
            "provider_cost_usd": round(usage1 - usage0, 4) if usage0 is not None and usage1 is not None else None,
        }

    detail = {"claims": {k: [x["id"] for x in v] for k, v in match.items()},
              "unmatched_claims": [k for k, v in match.items() if not v], "questions": rows}
    return {"track": "synthetic", "metrics": m, "detail": detail}


# ── real track (your own mail, via Composio) ─────────────────────────────────

_QGEN_SYS = ("You write test questions for a company-memory system. Given ONE fact, write one natural "
             "question a colleague might ask whose answer is that fact. Do NOT copy wording from the "
             "fact: use different words, and do not include the answer in the question. "
             'Reply JSON only: {"question": "..."}')

_GENERIC_NEGATIVES = [
    "What is the company's Net Promoter Score?", "Who won the last company hackathon?",
    "What is the CEO's home address?", "How many customers churned last quarter?",
    "What is our refund policy?", "Which cloud region hosts the staging database?",
    "What was the total revenue in 2019?", "Who is the head of legal?",
]


def run_real(args) -> dict:
    import datetime as dt
    from brain.core import db, db_pg, ingest
    from brain.core.connectors.composio import ComposioConnector
    from brain.engine import Brain

    ws = "ws_e2e_real"
    conn = db.connect()
    db.migrate(conn)
    db_pg._set_ws(conn, ws)
    reset_workspace(conn, ws)
    db.ensure_team(conn, ws, "personal", "Personal")
    rng = random.Random(args.seed)
    usage0 = _openrouter_usage()
    m, private = {}, {}

    with embedding_guard():
        # 1. pull the mail
        since = dt.datetime.utcnow() - dt.timedelta(days=args.days)
        t0 = time.perf_counter()
        eps = ComposioConnector(app="gmail", user_id=args.owner).poll(since=since)
        t_pull = time.perf_counter() - t0
        inserted = ingest.run(conn, ws, eps, team_id="personal", trigger="backfill")
        t0 = time.perf_counter()
        n_facts, rounds = _drain_extraction(conn, ws)
        t_extract = time.perf_counter() - t0

        cur = conn.cursor()
        cur.execute("""SELECT f.id, f.summary, f.invalidated_at, f.visibility, f.owner_id, e.body
                       FROM facts f JOIN episodes e ON e.id = f.episode_id WHERE f.workspace_id=%s""", (ws,))
        facts = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS n FROM episodes WHERE workspace_id=%s", (ws,))
        n_eps = cur.fetchone()["n"]
        cur.execute("SELECT type, COUNT(*) AS n FROM fact_edges WHERE workspace_id=%s GROUP BY type", (ws,))
        edges = {r["type"]: r["n"] for r in cur.fetchall()}
        cur.close()

        left_unextracted = _unextracted(conn, ws)
        m["ingestion"] = {"emails_fetched": len(eps), "episodes_stored": n_eps,
                          "dropped_or_duplicate": len(eps) - n_eps}
        with_facts = len({f["body"] for f in facts})
        m["extraction"] = {
            "facts": len(facts), "facts_per_episode": round(len(facts) / max(1, n_eps), 2),
            "episodes_with_no_fact": n_eps - with_facts,
            "private_by_default": prop(sum(f["visibility"] == "private" for f in facts), len(facts)),
        }
        # groundedness on a sample (all facts if few)
        sample = rng.sample(facts, min(len(facts), args.judge_sample))
        sup = [judge_supported(f["body"], f["summary"]) for f in sample]
        sup = [s for s in sup if s is not None]
        m["extraction"]["groundedness"] = prop(sum(sup), len(sup))
        m["reconcile"] = {"supersedes_edges": edges.get("supersedes", 0),
                          "in_tension_edges": edges.get("in_tension_with", 0),
                          "stale_facts": sum(f["invalidated_at"] is not None for f in facts)}

        # 2. questions generated from facts, answered by the system as the owner
        live = [f for f in facts if f["invalidated_at"] is None]
        picks = rng.sample(live, min(len(live), args.questions))
        qs = []
        for f in picks:
            r = _judge_json(_QGEN_SYS, f"FACT: {f['summary']}")
            if r.get("question"):
                qs.append({"q": r["question"], "gold": f["id"], "fact": f["summary"]})
        brain = Brain(workspace_id=ws, team_id="personal", caller=args.owner,
                      principals=[args.owner, "everyone"])
        ranks, lat, faith_k, faith_n = [], [], 0, 0
        cites = {"total": 0, "valid": 0, "judged": 0, "supported": 0}
        for q in qs:
            t0 = time.perf_counter()
            res = brain.search(q["q"], surface="eval")
            lat.append(time.perf_counter() - t0)
            rf = res.get("facts", [])
            ranks.append(rank_metrics([x["fact_id"] for x in rf], [q["gold"]]))
            text = (res.get("synthesized_answer") or "").strip()
            if text:
                fth = judge_faithful(q["q"], text, rf)
                if fth is not None:
                    faith_n += 1
                    faith_k += bool(fth)
                c = citation_scores(text, rf)
                for kk in cites:
                    cites[kk] += c[kk]
        refused = 0
        for nq in _GENERIC_NEGATIVES:
            res = brain.search(nq, surface="eval")
            text = (res.get("synthesized_answer") or "").strip()
            refused += (not res.get("facts")) or bool(_ABSTAIN_RE.search(text)) or \
                "reliable information" in (res.get("answer") or "")
        m["retrieval"] = {
            "questions": len(qs),
            "hit@5": prop(int(sum(x["hit"] for x in ranks)), len(ranks)),
            "mrr": mean([x["mrr"] for x in ranks]), "ndcg@5": mean([x["ndcg"] for x in ranks]),
        }
        m["answers"] = {
            "faithfulness": prop(faith_k, faith_n),
            "citation_validity": prop(cites["valid"], cites["total"]),
            "citation_support": prop(cites["supported"], cites["judged"]),
            "abstention_on_out_of_scope": prop(refused, len(_GENERIC_NEGATIVES)),
        }
        usage1 = _openrouter_usage()
        m["health"] = _health(left_unextracted, len(qs), len(qs) - faith_n)
        m["ops"] = {"pull_seconds": round(t_pull, 1), "extract_seconds": round(t_extract, 1),
                    "search_latency_s": {"p50": pct(lat, .5), "p95": pct(lat, .95)},
                    "provider_cost_usd": round(usage1 - usage0, 4) if usage0 is not None and usage1 is not None else None}

        # 3. raw text for the owner's own review stays local
        private = {"questions": qs,
                   "unsupported": [f["summary"] for f, s in zip(sample, sup) if s is False][:50]}
        audit = []
        cur = conn.cursor()
        cur.execute("""SELECT fe.id, fe.type, fa.summary AS newer, fb.summary AS older
                       FROM fact_edges fe JOIN facts fa ON fa.id=fe.src_fact JOIN facts fb ON fb.id=fe.dst_fact
                       WHERE fe.workspace_id=%s AND fe.type IN ('supersedes','in_tension_with')""", (ws,))
        audit = [dict(r) for r in cur.fetchall()]
        cur.close()
        private["audit_edges"] = audit
    return {"track": "real", "metrics": m, "private": private}


# ── output ───────────────────────────────────────────────────────────────────

def _fmt(v):
    if isinstance(v, dict) and "k" in v:
        if not v["n"]:
            return "n/a"
        lo, hi = v["ci95"]
        return f"{v['value']:.3f} ({v['k']}/{v['n']}, CI {lo:.2f}-{hi:.2f})"
    return str(v)


def print_report(res: dict):
    h = res["metrics"].get("health", {})
    banner = "VALID" if h.get("valid", True) else "PARTLY INVALID"
    print(f"\n=== end-to-end benchmark - {res['track']} - {banner} ===")
    for prob in h.get("problems", []):
        print(f"  !! {prob}")
    bad = [k for k, ok in h.get("stages", {}).items() if not ok]
    if bad:
        print(f"  !! ignore these sections (not a measurement of the system): {', '.join(bad)}")
    for section, vals in res["metrics"].items():
        print(f"\n[{section}]")
        for k, v in vals.items():
            if isinstance(v, dict) and "k" not in v:
                for k2, v2 in v.items():
                    print(f"  {k + '.' + k2:<44} {_fmt(v2)}")
            else:
                print(f"  {k:<44} {_fmt(v)}")


def pool(results: list) -> dict:
    """Pool proportions across runs (sum counts) and average plain numbers."""
    def walk(items):
        first = items[0]
        if isinstance(first, dict) and "k" in first and "n" in first:
            k, n = sum(i["k"] for i in items), sum(i["n"] for i in items)
            vals = [i["value"] for i in items if i["value"] is not None]
            lo, hi = wilson(k, n)
            return {"k": k, "n": n, "value": round(k / n, 4) if n else None, "ci95": [lo, hi],
                    "range": [min(vals), max(vals)] if vals else None}
        if isinstance(first, dict):
            return {key: walk([i[key] for i in items if key in i]) for key in first}
        nums = [i for i in items if isinstance(i, (int, float))]
        return round(sum(nums) / len(nums), 4) if nums else first
    return walk([r["metrics"] for r in results])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synthetic")
    s.add_argument("--out", default=str(HERE / "results" / "e2e_synthetic.json"))
    s.add_argument("--allow-remote", action="store_true")
    s.add_argument("--skip-preflight", action="store_true")
    s.add_argument("--reuse", action="store_true",
                   help="skip ingest/extract and re-score the questions against the facts already stored "
                        "in this run's workspace (same BENCH_WS_SUFFIX)")
    r = sub.add_parser("real")
    r.add_argument("--owner", required=True, help="Composio user id of the mailbox to evaluate")
    r.add_argument("--days", type=int, default=90)
    r.add_argument("--questions", type=int, default=40)
    r.add_argument("--judge-sample", type=int, default=150)
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--out", default=str(HERE / "results" / "e2e_real.json"))
    r.add_argument("--allow-remote", action="store_true")
    r.add_argument("--skip-preflight", action="store_true")
    a = sub.add_parser("aggregate")
    a.add_argument("files", nargs="+")
    a.add_argument("--out")
    args = ap.parse_args(argv)

    if args.cmd == "aggregate":
        runs = [json.loads(Path(f).read_text(encoding="utf-8")) for f in args.files]
        pooled_metrics, used = {}, {}
        for section in runs[0]["metrics"]:
            if section == "health":
                continue
            stage = SECTION_STAGE.get(section)
            ok_runs = [r for r in runs
                       if stage is None or r["metrics"].get("health", {}).get("stages", {}).get(stage, True)]
            used[section] = f"{len(ok_runs)}/{len(runs)} runs"
            for f, r in zip(args.files, runs):
                if r not in ok_runs:
                    print(f"{section}: skipping {f} (stage '{stage}' was not valid)", file=sys.stderr)
            if ok_runs:
                pooled_metrics[section] = pool([{"metrics": {section: r["metrics"][section]}} for r in ok_runs])[section]
        if not pooled_metrics:
            print("no valid sections to aggregate", file=sys.stderr)
            return 2
        pooled = {"track": runs[0]["track"], "n_runs": len(runs), "runs_used_per_section": used,
                  "metrics": pooled_metrics}
        if args.out:
            Path(args.out).write_text(json.dumps(pooled, indent=2), encoding="utf-8")
        print_report(pooled)
        print("runs used per section:", used)
        print(f"\npooled over {len(runs)} runs")
        return 0

    _require_local(args.allow_remote)
    from dotenv import load_dotenv
    load_dotenv()
    from brain import config
    meta = {"date": datetime.datetime.utcnow().strftime("%Y-%m-%d"), "git": git_rev(),
            "extract_model": config.EXTRACT_MODEL, "synth_model": config.SYNTH_MODEL,
            "judge_model": JUDGE_MODEL, "embed_model": config.EMBED_MODEL,
            "related_threshold": config.RELATED_THRESHOLD, "reconcile_mode": config.RECONCILE_MODE}
    if not args.skip_preflight:
        bad = preflight()
        if bad:
            print("Preflight failed, nothing was run:", file=sys.stderr)
            for b in bad:
                print(f"  - {b}", file=sys.stderr)
            print("Most often the provider account is out of credit (providers reserve credit for each "
                  "request's full output ceiling).", file=sys.stderr)
            return 3
    res = run_synthetic(args) if args.cmd == "synthetic" else run_real(args)
    res["meta"] = meta
    private = res.pop("private", None)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    if private:
        pdir = HERE / "results" / "private"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / f"e2e_real_{meta['date']}.json").write_text(
            json.dumps(private, indent=2, ensure_ascii=False), encoding="utf-8")
        with open(pdir / f"e2e_real_audit_{meta['date']}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["edge_id", "type", "newer_fact", "older_fact", "verdict", "notes"])
            w.writeheader()
            for e in private.get("audit_edges", []):
                w.writerow({"edge_id": e["id"], "type": e["type"], "newer_fact": e["newer"],
                            "older_fact": e["older"], "verdict": "", "notes": ""})
        print(f"private details (raw text) written under {pdir} — git-ignored")
    print_report(res)
    print(f"\nwrote {out}")
    return 0 if res["metrics"].get("health", {}).get("valid", True) else 2


if __name__ == "__main__":
    sys.exit(main())
