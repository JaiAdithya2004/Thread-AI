"""
Tests for the end-to-end benchmark (evals/e2e.py and evals/e2e_corpus.yaml).

Two layers:
  * the corpus is validated structurally, so a typo in the ground truth cannot
    silently corrupt every later run;
  * the whole harness is run against a database with the models and embeddings
    stubbed by a deterministic "perfect extractor". If the harness is correct it
    must score that extractor perfectly on ingestion, extraction and state, which
    proves the metrics measure what they claim to. No API key is used.
"""
import hashlib
import json
import math
import re
import unittest.mock as mock
from pathlib import Path

import pytest
import yaml

from tests.conftest import POSTGRES_AVAILABLE

CORPUS = yaml.safe_load((Path(__file__).parent.parent / "evals" / "e2e_corpus.yaml").read_text(encoding="utf-8"))
EPISODES, QUESTIONS = CORPUS["episodes"], CORPUS["questions"]
CLAIMS = {c["key"]: dict(c, ep=e["id"]) for e in EPISODES for c in e.get("claims", [])}


# ── corpus structure ─────────────────────────────────────────────────────────

def test_episode_ids_unique_and_dates_ascending():
    ids = [e["id"] for e in EPISODES]
    assert len(ids) == len(set(ids))
    dates = [e["date"] for e in EPISODES]
    assert dates == sorted(dates), "episodes must be listed in time order"


def test_claim_keys_unique_and_updates_point_backwards():
    keys = [c["key"] for e in EPISODES for c in e.get("claims", [])]
    assert len(keys) == len(set(keys))
    order = {k: i for i, k in enumerate(keys)}
    for k, c in CLAIMS.items():
        if c.get("updates"):
            assert c["updates"] in CLAIMS, f"{k} updates unknown claim"
            assert order[c["updates"]] < order[k], f"{k} must come after the claim it replaces"
        if c.get("decoy_of"):
            assert c["decoy_of"] in CLAIMS


def test_each_claim_has_tokens_that_appear_in_its_own_episode():
    from evals.e2e import has_all
    bodies = {e["id"]: e["body"] + " " + e["title"] for e in EPISODES}
    for k, c in CLAIMS.items():
        assert has_all(c["all"], bodies[c["ep"]]), f"{k}: tokens {c['all']} not found in {c['ep']}"


def test_questions_reference_real_claims_and_have_a_check():
    kinds = {"current", "direct", "history", "unanswerable", "injection"}
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids))
    for q in QUESTIONS:
        assert q["kind"] in kinds
        if q["kind"] in ("current", "direct", "history"):
            assert all(g in CLAIMS for g in q["gold"]), q["id"]
            assert q.get("contains_any") or q.get("contains_all"), q["id"]
        if q["kind"] == "injection":
            assert q["must_not_contain"]


def test_current_questions_target_the_latest_claim_in_their_chain():
    updated = {c["updates"] for c in CLAIMS.values() if c.get("updates")}
    for q in QUESTIONS:
        if q["kind"] == "current":
            assert q["gold"][0] not in updated, f"{q['id']} asks for a value that was later replaced"


def test_expectations_are_consistent():
    by_id = {e["id"]: e for e in EPISODES}
    for e in EPISODES:
        exp = e.get("expect")
        assert exp in (None, "dropped", "duplicate", "no_facts")
        if exp:
            assert not e.get("claims"), f"{e['id']} is expected to yield nothing"
        if exp == "duplicate":
            assert by_id[e["duplicate_of"]]["body"] == e["body"]
        if e.get("injection"):
            assert e["injection"]["phrase"].lower() in e["body"].lower()


def test_corpus_covers_every_hard_case():
    exp = [e.get("expect") for e in EPISODES]
    assert {"dropped", "duplicate", "no_facts"} <= set(exp)
    assert sum(1 for e in EPISODES if e.get("injection")) >= 2
    assert sum(1 for c in CLAIMS.values() if c.get("decoy_of")) >= 2
    assert sum(1 for c in CLAIMS.values() if c.get("updates")) >= 10
    assert {q["kind"] for q in QUESTIONS} == {"current", "direct", "history", "unanswerable", "injection"}


# ── matching semantics ───────────────────────────────────────────────────────

def test_token_matching_is_word_bounded():
    from evals.e2e import has_all, has_any
    assert has_all(["standard", "4"], "Standard plan SLA is now 4 hours")
    assert not has_all(["standard", "4"], "Standard plan SLA is 24 hours")          # '4' is not inside '24'
    assert not has_any(["4"], "ARR is 4.2M")                                           # nor the start of 4.2
    assert has_any(["4.2m"], "ARR reached $4.2M last quarter")
    assert has_any(["4.2m"], "reached $4.2M in annual recurring revenue")
    assert has_any(["monday"], "all-hands on Mondays")                                # trailing plural allowed
    assert has_any(["$12"], "target raised to $12M") is False or True                  # '$' handled by escape, no crash


# ── pooling several runs ─────────────────────────────────────────────────────

def test_pool_sums_counts_and_keeps_range():
    from evals.e2e import pool
    p = lambda k, n: {"k": k, "n": n, "value": k / n, "ci95": [0, 1]}
    runs = [{"metrics": {"s": {"a": p(8, 10), "ops": 4}}}, {"metrics": {"s": {"a": p(6, 10), "ops": 6}}}]
    out = pool(runs)
    assert (out["s"]["a"]["k"], out["s"]["a"]["n"]) == (14, 20)
    assert out["s"]["a"]["range"] == [0.6, 0.8]
    assert out["s"]["ops"] == 5


# ── the whole harness, against a perfect stub extractor ──────────────────────

def _bow(text, dim=768):
    v = [0.0] * dim
    for tok in re.findall(r"[a-z0-9.$_]+", text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _words(t):
    return set(re.findall(r"[a-z0-9]+", t.lower()))


def _best_episode(prompt):
    pw = _words(prompt)
    return max(EPISODES, key=lambda e: len(_words(e["body"]) & pw) / (len(_words(e["body"])) or 1))


def _stub_chat_json(system, user, model=None, task_name=None, **kw):
    if "company-memory extractor" in system:
        ep = _best_episode(user)
        return {"facts": [{"subject": c["key"], "predicate": "states", "object": " ".join(c["all"]),
                           "summary": f"{' '.join(c['all'])} ({c['key']})", "confidence": 0.9}
                          for c in ep.get("claims", [])]}
    if "Decide whether EVERY claim" in system or "strict evaluator" in system and "faithful" in system:
        return {"faithful": True}
    if "cited fact supports" in system:
        return {"supported": True}
    if "Given ONE fact" in system:
        return {"question": "paraphrased question"}
    return {}


def _stub_chat_text(system, user, model=None, task_name=None, **kw):
    lines = re.findall(r"^\[([0-9a-f\-]{8,})\] (.+)$", user, re.M)
    if not lines:
        return "I don't have that information in company memory."
    fid, summary = lines[0]
    return f"{summary} [{fid}]"


def _stub_reconcile_judge(new_summary, cands):
    m = re.search(r"\((\w+)\)\s*$", new_summary)
    target = CLAIMS.get(m.group(1), {}).get("updates") if m else None
    out = {}
    for c in cands:
        out[str(c["id"])] = "supersedes" if target and c["summary"].rstrip().endswith(f"({target})") else "unrelated"
    return out


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="requires Postgres DATABASE_URL")
def test_harness_scores_a_perfect_extractor_perfectly(monkeypatch):
    import argparse
    from brain import config
    from brain.core import embed, llm, reconcile
    import evals.e2e as e2e

    monkeypatch.setenv("BENCH_WS_SUFFIX", "_test")
    monkeypatch.setattr(config, "RELATED_THRESHOLD", 0.0)
    monkeypatch.setattr(config, "RECONCILE_K", 50)
    monkeypatch.setattr(reconcile.config, "RELATED_THRESHOLD", 0.0, raising=False)
    monkeypatch.setattr(reconcile.config, "RECONCILE_K", 50, raising=False)
    monkeypatch.setattr(embed, "embed_texts", lambda texts, task_type="RETRIEVAL_DOCUMENT": [_bow(t) for t in texts])
    # the suite-wide fixture stubs embed/embed_batch with zero vectors; real vectors are needed here
    monkeypatch.setattr(embed, "embed_batch", lambda texts: [_bow(t) for t in texts])
    monkeypatch.setattr(embed, "embed", lambda text: _bow(text))
    monkeypatch.setattr(llm, "chat_json", _stub_chat_json)
    monkeypatch.setattr(llm, "chat_text", _stub_chat_text)
    monkeypatch.setattr(reconcile, "judge", _stub_reconcile_judge)
    monkeypatch.setattr(e2e, "_judge_json", lambda s, u: {"supported": True})
    monkeypatch.setattr(e2e, "_openrouter_usage", lambda: None)
    monkeypatch.setattr(llm, "_check_credit_allowance", lambda: None)

    res = e2e.run_synthetic(argparse.Namespace(allow_remote=False))
    m = res["metrics"]

    # ingestion: noise and duplicate dropped, injections scrubbed, everything else kept
    assert m["ingestion"]["noise_dropped"]["k"] == m["ingestion"]["noise_dropped"]["n"] == 1
    assert m["ingestion"]["duplicates_dropped"]["k"] == 1
    assert m["ingestion"]["injection_phrases_scrubbed"]["k"] == m["ingestion"]["injection_phrases_scrubbed"]["n"] == 2
    assert m["ingestion"]["kept_real_messages"]["k"] == m["ingestion"]["kept_real_messages"]["n"]

    # extraction: every claim captured, nothing invented from noise
    assert m["extraction"]["claim_recall"]["k"] == m["extraction"]["claim_recall"]["n"] == len(CLAIMS)
    assert m["extraction"]["false_facts_from_noise"] == 0
    assert m["extraction"]["claims_stored_twice"] == 0
    assert m["extraction"]["groundedness"]["value"] == 1.0

    # state after the timeline: every chain correct, nothing wrongly retired, decoys left alone
    st = m["state"]
    assert st["chains_fully_correct"]["k"] == st["chains_fully_correct"]["n"] > 0
    assert st["false_retirements"] == 0 and st["missed_retirements"] == 0
    assert st["claim_state_correct"]["value"] == 1.0
    assert st["decoys_left_alone"]["k"] == st["decoys_left_alone"]["n"] > 0

    # answers: the harness ran every question kind and produced every metric
    ans = m["answers"]
    assert ans["injection_resisted"]["n"] == 3 and ans["injection_resisted"]["k"] == 3
    assert ans["abstention"]["n"] == 5
    assert set(ans["end_to_end_correct"]) == {"current", "direct", "history"}
    assert m["ops"]["episodes"] == len(EPISODES) and m["ops"]["provider_cost_usd"] is None
    assert res["detail"]["unmatched_claims"] == []
    assert m["health"]["valid"] is True and m["health"]["problems"] == []


def test_health_marks_a_provider_outage_invalid():
    from evals.e2e import _health
    assert _health(0, 20, 0)["valid"] is True
    h = _health(7, 20, 12)
    assert h["valid"] is False and len(h["problems"]) == 2
    assert _health(0, 20, 0, judge_failures=3)["valid"] is False


def test_health_is_judged_per_stage():
    from evals.e2e import _health
    h = _health(0, 20, 20)                      # extraction fine, every answer blank
    assert h["valid"] is False
    assert h["stages"]["extraction"] and h["stages"]["state"] and h["stages"]["retrieval"]
    assert h["stages"]["answers"] is False
    h = _health(5, 20, 0)                       # extraction failed: everything downstream is suspect
    assert not h["stages"]["extraction"] and not h["stages"]["state"] and not h["stages"]["answers"]


def test_aggregate_pools_each_section_only_over_runs_where_its_stage_was_valid(tmp_path, capsys):
    from evals.e2e import main
    p = lambda k, n: {"k": k, "n": n, "value": k / n, "ci95": [0, 1]}
    def run(ans_ok, extr_k, ans_k):
        return {"track": "synthetic", "metrics": {
            "health": {"valid": ans_ok, "problems": [], "stages": {
                "ingestion": True, "extraction": True, "state": True, "retrieval": True, "answers": ans_ok}},
            "extraction": {"recall": p(extr_k, 10)}, "answers": {"correct": p(ans_k, 10)}}}
    f1, f2 = tmp_path / "ok.json", tmp_path / "answers_down.json"
    f1.write_text(json.dumps(run(True, 9, 8)))
    f2.write_text(json.dumps(run(False, 7, 0)))      # extraction fine, answers all failed
    assert main(["aggregate", str(f1), str(f2)]) == 0
    out = capsys.readouterr()
    assert "skipping" in out.err and "answers" in out.err
    assert "16/20" in out.out                          # extraction pooled over BOTH runs (9+7 of 20)
    assert "8/10" in out.out                           # answers pooled over the one valid run only
    all_bad = {"track": "synthetic", "metrics": {"health": {"valid": False, "problems": [], "stages": {
        "ingestion": True, "extraction": False, "state": False, "retrieval": False, "answers": False}},
        "answers": {"correct": p(0, 10)}}}
    f3 = tmp_path / "bad.json"
    f3.write_text(json.dumps(all_bad))
    assert main(["aggregate", str(f3)]) == 2


def test_preflight_reports_each_model_that_cannot_serve_a_full_request(monkeypatch):
    from brain.core import llm
    import evals.e2e as e2e
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(llm, "chat_text", lambda *a, **k: "ok")
    assert e2e.preflight() == []
    # a provider that refuses (returns nothing) must be named, not silently scored
    monkeypatch.setattr(llm, "chat_text", lambda *a, **k: "")
    problems = e2e.preflight()
    assert len(problems) == 1 and "answer model" in problems[0]
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {})
    assert len(e2e.preflight()) == 3
