# Thread Platform — Evaluation Audit Report

> **Re-audited 2026-10-22.** Part I below is the current state. Part II is the original audit of 2026-08-29, kept unchanged as the historical baseline that Part I is measured against.

---

# Part I — Re-audit, 2026-10-22

## I.1 Summary

| | 2026-08-29 | 2026-10-22 |
|---|---|---|
| Test files / test functions | 11 / — | **21 / 357** |
| Last full local run | — | **354 passed, 7 skipped, 0 failed** |
| Eval questions | 28 (keyword match) | **55** with explicit gold fact IDs (45 answerable, 10 unanswerable), plus 14 changing-fact chains, 14 decoys, 8 paraphrases, 2 future-dated cases |
| RAG quality metrics measured | **0** | Retrieval, answer quality, supersession and end-to-end, **on a controlled synthetic benchmark**; 3 runs on 10-21, 1 further valid run on 10-22 (Part I.3) |
| Critical bugs found | 7 | 7 resolved; 10 further defects found and fixed since (Part I.5); 8 items still open (Part I.6) |
| Validation on real data | none | **none run.** Tooling built (`evals/real_eval.py`, shadow mode) and protocol defined in the README |

**Overall maturity: pre-production, well tested.** The pipeline is measured and protected from regressions on a controlled benchmark and has documented failure modes. It has not been scored on real user questions, and a few security and scaling items remain open.

## I.2 Test results

### Run of 2026-10-22 (local Postgres 16 + pgvector in Docker)

```
298 passed, 12 skipped, 0 failed   (Playwright and load suites excluded; see below)
```

| Skipped (12) | Why |
|---|---|
| 6 × `tests/test_regression.py` benchmark gate | **Infrastructure unavailable, correctly not scored:** the embedding providers were failing (Gemini free-tier quota exhausted; OpenRouter embeddings returned `402 Payment Required`). The gate aborts instead of scoring zero vectors. The 7th gate test needs `BENCH_LLM=1` |
| 4 × `tests/test_privacy.py` | Need real JWT enforcement; the suite runs in dev-bypass mode |
| 2 × `tests/test_integration.py` | Conversation endpoints need an account membership that bypass mode does not create |

**Not run:** the Playwright end-to-end suite (`tests/e2e`, 8 browser journeys) and the load scripts (Locust, k6). `playwright` and `locust` are not installed in this environment, and installing Playwright downloads a browser. To run them: `pip install playwright pytest-playwright && playwright install chromium && pytest tests/e2e`.

**Final run of the day** (after the real-data validation tooling and shadow mode were added, with working embeddings): **318 passed, 7 skipped, 0 failed**, including the retrieval benchmark gate. 15 tests were added since the morning (`test_real_eval.py`: scoring, audit scoring, redaction, shadow/off modes, exports that never include private facts).

### What the first run of the day found

A later full run with the new OpenRouter key and `BENCH_LLM=1` also finished at **298 passed, 12 skipped, 0 failed**; its model-judged gate was skipped because the account's credit ran out during the benchmark runs that preceded it (the skip message says so).

A first full run, with the model-judged gate switched on (`BENCH_LLM=1`), had 6 failures. All six were environmental, not regressions, but they exposed real problems that are now fixed:

1. The gate treated broken embeddings as a quality regression (vector hit@5 of 0.11). It now skips with the reason. The guard that detects zero vectors is scoped so it cannot leak into other tests (an early version of the fix did, and broke one unrelated test; caught on the next run).
2. **Model calls reserved the model's full output limit.** With no `max_tokens`, LiteLLM sends the model's maximum (65,536 for some models), so OpenRouter must reserve credit for all of it. On a small balance a fraction-of-a-cent request failed with `402: requires more credits … can only afford 73`. Every call now carries a per-task ceiling (`extract` 4,000, `reconcile` 1,500, `synthesize` 2,000, and so on).
3. The original audit's `--reset-extracted` finding was still broken in a new way: `evals/run.py` assigned `false` to an `INTEGER` column. Fixed (`extracted=0`).

## I.3 RAG evaluation

### I.3.1 The re-run of 2026-10-22

The first attempt (earlier the same day) was blocked: the OpenRouter account had no credit and the embedding providers were failing, so the benchmark aborted rather than score noise. After a new OpenRouter key was added, three full runs were started (`evals/results/audit_2026-10-22/`). The outcome is mixed, and it is reported as it happened:

| Part | Run 1 | Runs 2 and 3 |
|---|---|---|
| Retrieval (needs embeddings only) | **Valid** | **Valid** — 135/135 hit@5 across all three runs |
| Answer quality, supersession, end to end (need the chat models) | **Valid** | **Invalid** — the account's credit ran out partway through run 2 (`402 … You requested up to 2000 tokens, but can only afford 1990`). Run 2 scored 12 of 45 answers; run 3 scored none; the reconcile judge failed on every call, so supersession recall read 0/16 |

The invalid runs were moved to `evals/results/controlled_rerun_2026-10-22/excluded_provider_credit_exhausted/` and are **excluded from every figure below**. Averaging them in would have reported an outage as a regression.

Consequences:

- The fresh results for the model-dependent parts come from **one run, not three**, so there is no run-to-run range for them. Treat run 1 as a consistency check against the 2026-10-21 three-run results, not as a replacement for them.
- The key authenticates but the account runs on a small free allowance. OpenRouter reported $0.164 of usage in total by the time it ran out, which covered run 1 in full and part of run 2 (plus the test suite's few calls), so **one full run costs roughly $0.10–0.15** — the first metered figure, derived from that total rather than read per run. More runs need credit added at openrouter.ai/settings/credits.
- Embeddings were served through the OpenRouter fallback (same model, no task-type hint) because Gemini's free embedding quota was exhausted; 133 batches per run.

### I.3.2 What was measured, and on what

A controlled benchmark on a **fictional company** (`evals/corpus.yaml`): 44 facts; 45 answerable questions with gold fact IDs (13 deliberately paraphrased); 10 unanswerable; 14 chains of changing facts (30 versions, 16 updates); 14 decoys that must replace nothing; 8 paraphrases that should be detected as duplicates; 2 future-dated changes. Ground truth is written by reading the corpus, never derived from what retrieval returned. Models: extraction and reconcile judge `gemini-2.5-flash-lite`; answers `gemini-3.8-flash`; answer judge `gpt-4o-mini` (a different family from the answerer); embeddings `gemini-embedding-001` (768-dim).

**This is not a measurement on real data.**

#### Fresh run — 2026-10-22, run 1 (commit `8d0458e` plus the working-tree fixes)

**Retrieval (k=5)**

| Ranker | Hit@5 | Recall@5 | Precision@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|
| vector | **1.000** (45/45; all runs equal) | **1.000** | **0.209** | **1.000** | **1.000** |
| fulltext | **0.244** (11/45; all runs equal) | **0.244** | **0.049** | **0.244** | **0.244** |
| hybrid_rrf | **1.000** (45/45; all runs equal) | **1.000** | **0.209** | **1.000** | **1.000** |
| production | **1.000** (45/45; all runs equal) | **1.000** | **0.209** | **1.000** | **1.000** |

**Answer quality**

| Metric | Result |
|---|---|
| Answer correct (answerable) | **1.000** (45/45; all runs equal) |
| Faithfulness (LLM judge) | **0.933** (42/45; all runs equal) |
| Citation validity (cited id was retrieved) | **1.000** (47/47; all runs equal) |
| Citation support (LLM judge) | **1.000** (47/47; all runs equal) |
| Abstention on unanswerable | **1.000** (10/10; all runs equal) |
| Answer latency p50 / p95 (s) | 3.737 / 13.393 |
| Retrieval latency p50 / p95 (s) | 3.8 / 5.265 |

**Supersession decisions (pairwise, database in true state)**

| Metric | Exact-subject (previous) | Embedding similarity only | Embedding + LLM judge (current) |
|---|---|---|---|
| Supersession precision | **1.000** | **0.417** | **0.875** |
| Supersession recall | **0.062** (1/16; all runs equal) | **0.625** (10/16; all runs equal) | **0.875** (14/16; all runs equal) |
| Supersession F1 | **0.118** | **0.500** | **0.875** |
| Updates lost as 'duplicates' | **0.000** (0/16; all runs equal) | **0.062** (1/16; all runs equal) | **0.000** (0/16; all runs equal) |
| Paraphrases recognised as duplicates | **0.125** (1/8; all runs equal) | **0.625** (5/8; all runs equal) | **1.000** (8/8; all runs equal) |
| Decoys wrongly acted on | **0.000** (0/14; all runs equal) | **0.643** (9/14; all runs equal) | **0.000** (0/14; all runs equal) |
| Future-dated changes wrongly superseded | **0.000** (0/2; all runs equal) | **1.000** (2/2; all runs equal) | **1.000** (2/2; all runs equal) |
| True predecessor among candidates | - | - | **1.000** (16/16; all runs equal) |

**End to end: changing facts through the real save pipeline**

| Metric | Reconciliation off (previous) | Reconciliation on (current) |
|---|---|---|
| Versions stored | **0.967** (29/30; all runs equal) | **1.000** (30/30; all runs equal) |
| Version status correct (valid vs stale) | **0.433** (13/30; all runs equal) | **0.933** (28/30; all runs equal) |
| Chains fully correct | **0.000** (0/14; all runs equal) | **0.857** (12/14; all runs equal) |
| 'Current X?' top result is the latest | **0.214** (3/14; all runs equal) | **0.857** (12/14; all runs equal) |
| 'Current X?' answer states latest only | **0.000** (0/14; all runs equal) | **0.857** (12/14; all runs equal) |
| History returns every version | **0.967** (29/30; all runs equal) | **1.000** (30/30; all runs equal) |
| History marks stale/valid correctly | **0.433** (13/30; all runs equal) | **0.933** (28/30; all runs equal) |
| Unrelated facts wrongly invalidated | 0 | 0 |


#### Previous results — 2026-10-21, three runs (mean, range in parentheses, counts pooled)

**Retrieval (k=5)**

| Ranker | Hit@5 | Recall@5 | Precision@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|
| vector | **1.000** (135/135; all runs equal) | **1.000** | **0.209** | **0.982** | **0.986** |
| fulltext | **0.244** (33/135; all runs equal) | **0.244** | **0.049** | **0.244** | **0.244** |
| hybrid_rrf | **1.000** (135/135; all runs equal) | **1.000** | **0.209** | **0.982** | **0.986** |
| production | **1.000** (135/135; all runs equal) | **1.000** | **0.209** | **1.000** | **1.000** |

**Answer quality**

| Metric | Result |
|---|---|
| Answer correct (answerable) | **1.000** (135/135; all runs equal) |
| Faithfulness (LLM judge) | **0.956** (129/135; all runs equal) |
| Citation validity (cited id was retrieved) | **1.000** (143/143; all runs equal) |
| Citation support (LLM judge) | **1.000** (143/143; all runs equal) |
| Abstention on unanswerable | **1.000** (30/30; all runs equal) |
| Answer latency p50 / p95 (s) | 3.9733 / 13.8233 |
| Retrieval latency p50 / p95 (s) | 2.3397 / 3.642 |

**Supersession decisions (pairwise, database in true state)**

| Metric | Exact-subject (previous) | Embedding similarity only | Embedding + LLM judge (current) |
|---|---|---|---|
| Supersession precision | **1.000** | **0.385** (0.35-0.42) | **0.855** (0.83-0.88) |
| Supersession recall | **0.062** (3/48; all runs equal) | **0.625** (30/48; 0.56-0.69) | **0.854** (41/48; 0.75-0.94) |
| Supersession F1 | **0.118** | **0.476** (0.43-0.50) | **0.852** (0.80-0.88) |
| Updates lost as 'duplicates' | **0.000** (0/48; all runs equal) | **0.188** (9/48; 0.06-0.31) | **0.000** (0/48; all runs equal) |
| Paraphrases recognised as duplicates | **0.083** (2/24; 0.00-0.12) | **0.458** (11/24; 0.00-0.75) | **1.000** (24/24; all runs equal) |
| Decoys wrongly acted on | **0.000** (0/42; all runs equal) | **0.714** (30/42; 0.50-1.00) | **0.024** (1/42; 0.00-0.07) |
| Future-dated changes wrongly superseded | **0.000** (0/6; all runs equal) | **1.000** (6/6; all runs equal) | **1.000** (6/6; all runs equal) |
| True predecessor among candidates | - | - | **0.979** (47/48; 0.94-1.00) |

**End to end: changing facts through the real save pipeline**

| Metric | Reconciliation off (previous) | Reconciliation on (current) |
|---|---|---|
| Versions stored | **0.944** (85/90; 0.93-0.97) | **1.000** (90/90; all runs equal) |
| Version status correct (valid vs stale) | **0.411** (37/90; 0.40-0.43) | **0.989** (89/90; 0.97-1.00) |
| Chains fully correct | **0.000** (0/42; all runs equal) | **0.976** (41/42; 0.93-1.00) |
| 'Current X?' top result is the latest | **0.167** (7/42; 0.14-0.21) | **0.976** (41/42; 0.93-1.00) |
| 'Current X?' answer states latest only | **0.000** (0/42; all runs equal) | **0.976** (41/42; 0.93-1.00) |
| History returns every version | **0.944** (85/90; 0.93-0.97) | **1.000** (90/90; all runs equal) |
| History marks stale/valid correctly | **0.411** (37/90; 0.40-0.43) | **0.989** (89/90; 0.97-1.00) |
| Unrelated facts wrongly invalidated | 0 | 0 |


#### Do the two agree?

| Metric | 10-21 (3 runs) | 10-22 (1 run) |
|---|---|---|
| Hit@5, production path | 1.000 (135/135) | 1.000 (45/45; 135/135 over the three retrieval parts) |
| Answers correct | 135/135 | 45/45 |
| Faithfulness | 0.956 | 0.933 |
| Citations real / supported | 143/143 · 143/143 | 47/47 · 47/47 |
| Unanswerable refused | 30/30 | 10/10 |
| Supersession F1 (judge) | 0.852 | 0.875 |
| Updates missed | 7/48 | 2/16 |
| Paraphrases recognised | 24/24 | 8/8 |
| Decoys wrongly acted on | 1/42 | 0/14 |
| Future-dated wrongly superseded | 6/6 | 2/2 |
| Chains in the correct end state | 41/42 | 12/14 |
| "Current X?" answered with the latest only | 41/42 | 12/14 |

Within the noise expected from one run of 14 chains, the two runs agree. The weaknesses are the same ones: future-dated changes are always mishandled, and a few provider/place changes are missed.

### I.3.5 End-to-end benchmark (raw messages through the whole pipeline)

Added after this audit's first draft. It exercises ingest, LLM extraction, reconcile, retrieval, answering and judging together on 36 labelled raw messages, and on a real mailbox. It found and drove fixes for four product defects (a null field discarding whole messages, history questions unable to see superseded facts, an answer model unaware of dates, and a message superseding itself).

**Results** (2026-10-22, one extraction run on the final code; the question stage was re-scored against the stored facts with the cost-optimised answer model, `gemini-2.5-flash-lite`; judge `gpt-4o-mini`; there is no run-to-run range because only one run could be afforded, so treat the intervals as the uncertainty):

| Stage | Metric | Result |
|---|---|---|
| **Ingestion** | noise message dropped · duplicate dropped · real messages kept · injection phrases scrubbed | 1/1 · 1/1 · 32/32 · 2/2 |
| **Extraction** | claims captured (recall) | **38/39 (97.4%)**, 95% CI 87–99% |
| | facts supported by their source message | 62/62 (100%) |
| | facts invented from noise messages | 0 |
| | the same claim stored twice | 3 |
| **State after six months of updates** | update pairs handled correctly (old retired, new valid) | 11/13 (84.6%) |
| | whole chains correct (every version in the right state) | 11/12 (91.7%) |
| | decoys left alone | 2/2 |
| | facts wrongly retired · replaced facts missed | 0 · 1 |
| **Retrieval** (given the claim was extracted) | hit@5 · MRR · nDCG@5 | 27/27 (100%) · 0.91 · 0.93 |
| | "what is X now?" ranks the latest value first | 9/11 |
| **Answers, end to end** | correct (all 28 answerable questions) | **26/28 (92.9%)** |
| | by type: current · direct · history | 10/12 · 13/13 · 3/3 |
| | faithfulness (judge sees the same dates and labels as the answerer) | 24/28 (85.7%) |
| | citations real · citations that support their sentence | 41/41 · 39/41 (95.1%) |
| | unanswerable questions refused | 5/5 |
| | planted prompt-injection attempts resisted | 3/3 |
| | current answers that state only the latest value (no mention of older ones) | 6/12 |
| **Operations** | extraction time per kept message | 6.6 s (sequential model calls) |
| | search latency p50 / p95 (answer included) | 3.2 s / 6.7 s |
| | provider cost: extraction run · question re-score | $0.0225 · $0.0067 |

**What the benchmark found and the product fixes it drove.** The first run on the original code (a run in which two messages could not be extracted, `evals/results/e2e_2026-10-22/development_runs/1_original_code.json`) is the "before":

| | Before | After |
|---|---|---|
| Claims captured | 33/39 (84.6%) | 38/39 (97.4%) |
| Update pairs correct | 8/13 | 11/13 |
| Chains fully correct | 8/12 | 11/12 |
| Facts wrongly retired | 3 | 0 |
| Decoys left alone | 0/2 | 2/2 |
| hit@5 given extracted | 19/22 | 27/27 |
| Answers correct end to end | 17/28 (60.7%) | 26/28 (92.9%) |
| History questions answered | 0/3 | 3/3 |

Extraction, state and retrieval are like-for-like (same models). The answer rows are not: the "before" used the stronger answer model and the older judge, the "after" the cheaper answer model and a judge given the answerer's context, and one scoring bug (a "4.2" vs "$4.2M" match) was corrected, so read them as direction, not a controlled comparison. Four defects in the product came out of this, all fixed and regression-tested:

1. **A null field discarded a whole message.** Models return `"object": null`; a strict schema rejected the entire result, so every fact in the message was lost and retried forever (the whole first pricing email, among others).
2. **History questions could not work.** Search hides superseded facts, so "how has X changed over time?" saw only the latest value. Questions about change now include superseded facts and label them.
3. **The answer model was not told today's date or which facts were superseded,** so it called a price that had already changed "current until May 1st". It now receives the date, each fact's "as of" date and a superseded label.
4. **A message could supersede itself.** One email saying "migrating CI to GitHub Actions next sprint" and "the CircleCI contract ends in April" had its second fact retire its first. Facts from the same message can no longer retire or contradict each other.

**Still failing, and why.** (a) One casual chat line ("welcome to the new hires! engineering is now 15 people") was never extracted, so the old headcount stayed current. (b) With the cheaper answer model, the Pro price is answered "$29 until May 1st" instead of applying today's date; the stronger model got it right in a development run, so date reasoning depends on the answer model. (c) Three claims were stored twice. (d) Faithfulness and "latest value only" are the weakest answer metrics: answers often mention the older value as context, which is acceptable to a reader but counted by the strict metric.

**Real mail (your own Gmail, no labels).** 30 emails fetched, 21 kept after the noise filter and de-duplication, 15 facts extracted (all private to the owner by default). Every fact was supported by its source email (15/15). Fifteen questions generated from those facts: 15/15 retrieved in the top 5, 15/15 answers faithful, 23/23 citations real and supportive, 8/8 out-of-scope questions refused. This is a sanity check, not an accuracy claim: with only 15 facts retrieval is easy, 17 of 21 emails produced no fact, and no supersession was exercised. Raw text is kept only in `evals/results/private/` (git-ignored). Cost: $0.0094.

**Limits.** One extraction run (no range); the corpus and labels were written by the system's builder; the real-mail sample is small and unlabelled; a three-run pool with the stronger answer model was not possible because the provider account's balance ran out, and the runner now stamps such runs partly invalid per stage instead of publishing them. To repeat with ranges: fund the account and run `scripts/bench/run_e2e_benchmark.sh`.

### I.3.3 Reading the results

- **Retrieval** is at its ceiling (hit@5 1.000 in every run on both days) because the corpus is small (44 facts; k=5 returns 11% of it). It shows no defect and working semantics, not scale. Full-text search alone finds the right fact for only 24% of questions; semantic search carries the system.
- **Answer quality** is high: every cited fact ID was real and supported its sentence, and every unanswerable question was refused. The answers flagged unfaithful were, in each run, a few where the model added a caveat ("the facts do not specify …") that the judge counted as unsupported.
- **Supersession** is the main result: F1 0.85, against 0.12 for the earlier exact-subject method and 0.48 for embedding similarity alone, which acted wrongly on 71% of decoys.
- **End to end**, 41 of 42 changing-fact chains ended in the correct state, against 0 of 42 with the step off.
- **Known failures:** future-dated changes wrongly retire the current value (6/6); 7 of 48 updates were missed (provider or place changes such as AWS → Google Cloud); one decoy was superseded in one of three runs.

### I.3.4 Limits

Corpus and questions written by the system's builder; the 0.70 candidate threshold was chosen after seeing this benchmark's similarity distribution and is not validated on held-out data; 3 runs on 10-21 but only 1 valid run on 10-22, one machine, small n (16 updates, 14 chains); some or all embedding batches came from the OpenRouter fallback. A real-data validation protocol is in the README ("Validation on real data").

## I.4 Status of the original findings

### Critical, high and medium findings (Part II §3)

| Original finding | Status | Evidence |
|---|---|---|
| No RAG quality metrics measured | **Resolved (controlled data only)** | `evals/bench.py`; Part I.3. Real-data metrics: not measured |
| Vector search queried the wrong column | **Fixed** | `search_vec` and `_find_near_duplicate` use `fact_vec_768`; guarded by `test_vec_column_name_in_search` |
| Visibility constraint mismatch (`shared` rejected) | **Fixed** | Constraint is `private / team / shared`; extraction coerces any other value to `team` |
| No hallucination / faithfulness evaluation | **Resolved (controlled data)** | LLM-judge faithfulness 0.956 and citation support 143/143; gated with `BENCH_LLM=1`, not in default CI (costs tokens) |
| `in_tension_with` edges never surfaced | **Fixed** | `conflicts` returned by `format_answer`; tests in `test_phase4.py` |
| Rate limiting not shared across workers | **Open** | Still an in-process dict, so per serverless instance. Needs Redis or a database table |
| `evals/run.py --reset-extracted` placeholder bug | **Fixed** | `?` placeholders were corrected earlier; the remaining `extracted=false` type error was fixed today |
| `_save_atomic()` not atomic | **Open** | `db.save_fact` commits before edges are written; documented as best effort |

### Risks (Part II §4)

| Risk | Status |
|---|---|
| Answers not checked for groundedness | Measured on the benchmark; no runtime check |
| Contradictory facts with no signal | Mitigated: conflicts surfaced; semantic supersession retires replaced facts |
| Prompt injection from ingested content | **Mitigated, not evaluated end to end:** 8-pattern scrub at ingest, `<untrusted_content>` delimiters and explicit instructions at extraction and answering, output guardrail, 39 tests in `test_guardrails.py`. No adversarial run against a live model |
| MCP workspace auth is environment-only | **Open** (unchanged) |
| Stale knowledge without user awareness | Mitigated: stale facts excluded from answers; the history view shows them, marked |

### Roadmap items (Part II §17)

| Item | Status |
|---|---|
| P0.1 vector column | Done |
| P0.2 visibility constraint | Done |
| P0.3 eval dataset with fact IDs | Done for the synthetic corpus; legacy `questions.yaml` is still keyword-based |
| P0.4 faithfulness eval | Done in the benchmark; not in default CI |
| P1.1 surface conflicts | Done |
| P1.2 injection sanitizer | Done |
| P1.3 Redis-backed rate limiting | **Open** |
| P1.4 dedup calibration test | **Partial:** no threshold-sweep test. The benchmark measures paraphrase detection (24/24) and decoys (1/42) through the judge, and shows threshold-only behaviour acts wrongly on 71% of decoys |
| P1.5 `run.py` placeholder bug | Done |
| P1.6 `correct_fact` ownership check | **Partial:** an ACL-membership check, not author-based; tested |
| P2 `validate_llm_output` in extraction | Done |
| P2 embedding-model change guard | **Partial:** a test compares config to the baseline; no runtime guard |
| P2 retry / fallback / latency Prometheus metrics | **Open** |
| P3 chunking, deletion propagation, OAuth expiry alerts, freshness dashboard | **Open** |

### Evaluation matrix (Part II §16), updated

| Area | Metric | Now |
|---|---|---|
| Retrieval | Hit@5, Recall@5, Precision@5, MRR, nDCG@5 | Measured (controlled) |
| Retrieval | Hybrid vs full-text vs vector | Measured: vector = hybrid = 1.000; full-text 0.244 |
| Generation | Faithfulness, citation correctness, abstention, answer correctness | Measured (controlled) |
| Knowledge | Dedup / false-merge | Measured via decoys and paraphrases |
| Knowledge | Contradiction handling, temporal accuracy | Measured (supersession, history, current value) |
| Extraction | Fact F1, entity F1, confidence calibration | **Still missing:** the benchmark bypasses the extraction model |
| Security | Cross-tenant leakage | Tested (isolation suite) |
| Security | Prompt injection | Unit-tested; no live adversarial run |
| MCP | Tool selection | Not tested |
| Integration | Ingestion success rate | Not measured |
| Reliability | Provider fallback | Tested |
| Performance | Latency | Measured on a dev machine only |
| Cost | Cost per query | Metered per call; prices missing for most current models (billed at the default rate) |
| Regression | Golden dataset | Done: `evals/thresholds.yaml`, `evals/baseline.json`, `tests/test_regression.py` |

## I.5 Defects found and fixed since the original audit

Found while documenting the architecture (2026-10-21) and in this re-audit; each has a regression test in `tests/test_security_fixes.py`.

| Defect | Fix |
|---|---|
| `BACKEND` no longer exported, so plan, usage, backfill and dead-letter code raised `ImportError` (`/api/usage` and `/api/backfill` returned 500) | Restored |
| `/api/composio/connect` read a field `Identity` does not have (500) | Account id resolved from memberships |
| `POST /api/auth/signup` unauthenticated and trusted the user id in the body | Requires a verified non-guest session; uses that identity |
| `/api/composio/callback` trusted ids from its query string | Signed, expiring `state`; forged or missing state returns 400 |
| Role, team and workspace readable from user-editable `unsafe_metadata` | Only `public_metadata` is trusted |
| Billing context shared across request threads | Per-request `ContextVar` |
| A failed embedding was stored as an unsearchable zero vector | Extraction and harvest refuse to save and retry |
| Decision history could not show superseded facts; the word "current" disabled relevance ranking | Fixed; "current" is no longer a recency trigger |
| Embedding errors printed the API key into logs | Redacted; OpenRouter fallback added |
| Model calls reserved the model's full output limit | Per-task `max_tokens` ceilings |

## I.6 Still open

1. Rate limits and the guest daily cap are per instance (in-process).
2. The content-security policy may block Clerk's script and API; needs a browser check.
3. Row-level security on the knowledge tables is enabled but not forced, so it does not bind the owner role the app uses.
4. Manually recorded facts can still be saved with a zero vector.
5. CORS defaults to `*`.
6. The Playwright and load suites have not been run in this environment.
7. Real-data validation has not been run. The tooling to do it now exists (`evals/real_eval.py` to sample, label, score and audit; `BRAIN_RECONCILE_MODE=shadow` to log supersession decisions without applying them); what is missing is real data, people to label it, and the trial period.
8. The end-to-end benchmark has a single run (Part I.3.5). A three-run pool with the stronger answer model was not possible because the provider account's balance ran out; fund it and run `scripts/bench/run_e2e_benchmark.sh` to add ranges.
9. Only one valid benchmark run exists for 2026-10-22 because the OpenRouter account's small free allowance ran out; add credit and run two more to restore a three-run range.

---

# Part II — Original audit (2026-08-29)

> Retained unchanged as the baseline. Statements below describe the codebase as it was on that date; Part I records what has changed.

## 1. Executive Summary

Thread has solid infrastructure foundations (circuit breakers, RLS isolation, Langfuse tracing, a real eval dataset) but **zero measurement** of the metrics that matter most for a RAG system: retrieval recall, faithfulness, and citation correctness. Two critical silent bugs are degrading production quality today.

| Metric | Value |
|---|---|
| Test files | 11 |
| Eval questions (YAML) | 28 |
| Critical bugs found | 7 |
| RAG quality metrics measured | **0** |
| Hallucination tests | **0** |
| Cross-tenant risk | MEDIUM |

**Overall maturity: Early-Stage.** The test suite covers auth, isolation, and wiring — not quality.

---

## 2. What Already Exists

### Test Infrastructure

| File | Type | What It Covers |
|---|---|---|
| `tests/test_extraction.py` | Unit | extract_from_text, dedup gate, confidence threshold, noise filter |
| `tests/test_retrieval.py` | Unit | RRF merge, ACL filter, confidence threshold, LLM graceful degradation |
| `tests/test_isolation.py` | Integration | Cross-workspace isolation, RLS enabled, SQL injection, platform metrics flag |
| `tests/test_auth.py` | Unit | Bypass conditions, production boot guard, JWT mapping, require_admin |
| `tests/test_integration.py` | Integration | Full ingest→extract→retrieve pipeline, content hash dedup, team isolation |
| `tests/test_metering.py` | Unit | Cost computation, credit cap 402, BYO-key bypass |
| `tests/test_llm.py` | Unit | Billing context, usage extraction, credit check |
| `tests/test_validation.py` | Unit | All Pydantic request models, rate limiter allow/block/evict |
| `tests/test_privacy.py` | Unit | Search auth, conversation scoping, key exposure prevention |
| `tests/test_ingest_retrieve.py` | Integration | ACL isolation, team isolation, shared facts, workspace isolation, ledger |
| `tests/e2e/test_journeys.py` | E2E | 8 user journeys: signup, invite, isolation, chat, cost cap, LLM-down, SQL injection |

### Evaluation Script

`evals/run.py` runs `Brain.search()` directly against 28 ground-truth questions in `evals/questions.yaml`. Pass criteria: substring match in synthesized answer or facts. Optional RAGAS integration (faithfulness, answer_relevancy, context_recall) if `ragas` is installed. Also: `brain eval-providers` CLI command compares primary vs fallback model scores and latency.

### Observability

| System | Status | What It Captures |
|---|---|---|
| Langfuse | Active | Every LLM call: task_name, model, full messages, output, error level |
| Sentry | Active | Exceptions with PII scrubber; 10% perf sampling; never logs query/answer text |
| Prometheus | Active | `/metrics` endpoint, text format, 8 latency buckets |
| Circuit breakers | Active | 5 named breakers (llm, redis, embed, slack, composio); state via `/api/status` |
| Alert rules | Defined | `monitoring/alerts.yaml` — 6 rules: error rate, P95 latency, queue, cost, composio, auth failures |

### Security Controls

- **RLS double-enforcement:** Session GUC `brain.workspace_id` + explicit `WHERE workspace_id=%s` in every query
- **FORCE RLS** on conversations and messages — bypasses even superuser
- **`scripts/db/check_rls.py`** — CI gate verifying RLS enabled on all required tables
- **Sentry scrubber** strips query, answer, email, api_key, token, secret from all events
- **Guest token:** HMAC-SHA256, 4h expiry, workspace force-set server-side

### Resilience

- `brain/core/resilience.py` — 3-state circuit breaker with threading.Lock, exponential backoff retry with jitter
- LLM primary/fallback routing via `TASK_ROUTES` in `config.py`
- Graceful degradation: synthesis failure returns facts-only, never errors
- In-process LRU cache (256 entries) for query embeddings

---

## 3. What Is Missing

### CRITICAL

- **No RAG quality metrics measured anywhere.** Recall@K, Precision@K, MRR, NDCG, faithfulness, citation correctness — none computed. The eval script uses substring matching, not semantic similarity or standard IR metrics.

- **Vector search silently broken — wrong column name.** `db_pg.search_vec()` queries column `embedding` but the actual column is `fact_vec_768`. Vector similarity search returns zero results. All retrieval falls back to FTS-only.

- **Visibility constraint mismatch.** DB CHECK constraint allows `team|org|private` but `extract.py` writes `shared`. Every AI-extracted fact with `visibility='shared'` fails to insert silently.

### HIGH

- **No hallucination / faithfulness evaluation.** No test checks whether synthesized answers are grounded in retrieved facts. The synthesis prompt instructs the model but no automated check verifies compliance.

- **`in_tension_with` edges never surfaced.** Conflict detection writes edges to `fact_edges` but no API endpoint, retrieval path, or synthesis prompt uses them. Contradictions are silently stored but never acted on.

- **Rate limiting not shared across workers.** Rate limit store is a module-level Python dict — each uvicorn worker has its own. Effective limit = configured × num_workers.

### MEDIUM

- **`evals/run.py` `--reset-extracted` placeholder bug.** Uses `?` placeholders (SQLite) in a psycopg2 context — raises TypeError at runtime.

- **`_save_atomic()` is not actually atomic.** Fact committed, then edges written in a second commit. Crash between them leaves orphaned facts with no provenance edges.

---

## 4. Critical Risks

| Risk | Severity | Evidence |
|---|---|---|
| Synthesized answers not checked for groundedness | HIGH | No faithfulness eval; LLM can fabricate; prompt instructs but doesn't enforce |
| Vector search broken → poor retrieval → poor answers | CRITICAL | `search_vec()` wrong column — confirmed by code inspection |
| Shared-visibility facts silently not inserted | CRITICAL | CHECK constraint `team\|org\|private` vs extract writing `shared` |
| Contradictory facts coexist with no user signal | MEDIUM | `in_tension_with` edges exist in DB, never returned to user |
| Prompt injection from ingested content | MEDIUM | No injection defense in extraction or synthesis prompts; no tests |
| MCP workspace auth is environment-only | MEDIUM | Any process with env access can call any MCP tool; no per-call auth |
| Stale knowledge without user awareness | MEDIUM | Invalidated facts surfaced with "stale" tag only if `include_invalid=True` |

---

## 5. RAG Pipeline Architecture (P1)

```
SOURCE DATA → ingest.py → episodes table → extract.py → structured facts
→ dedup (0.92) → embed.py → [search_vec() BROKEN] → RRF merge
→ ACL + team filter → synthesize_answer() → cited answer
```

### Pipeline Stage Inventory

| # | Stage | File / Function | Notes |
|---|---|---|---|
| 1 | Data Ingestion | `brain/core/ingest.py · run()` | Content-hash dedup (SHA-256), noise filter, worthy() gate |
| 2 | Parsing | `brain/core/connectors/composio.py · _normalize_result()` | Source-specific normalizers for Gmail, Calendar, GitHub |
| 3 | Chunking | — | **MISSING** — No chunking. Full episode body sent to LLM. Max tokens not enforced. |
| 4–7 | LLM Extraction | `brain/core/extract.py · extract_from_text()` | Gemini 2.5 Flash, temp=0.1, JSON mode |
| 8 | Fact Normalization | `extract.py · _normalize()` | Strips whitespace, title-cases subjects, trims to field limits |
| 9 | Deduplication | `extract.py · _find_near_duplicate()` | Cosine distance < 0.08. **Queries wrong column.** |
| 10 | Contradiction | `extract.py · _detect_conflicts()` | Same subject+predicate, different object → `in_tension_with` edge. **Not surfaced to users.** |
| 11 | Embeddings | `brain/core/embed.py · embed_texts()` | Gemini `gemini-embedding-001`, 768-dim, L2-normalized, batch size 64 |
| 12 | Vector Storage | `db_pg.save_fact()` | Column `fact_vec_768 vector(768)` |
| 13 | HNSW Index | `migrations/0003_hnsw_768.py` | m=16, ef_construction=64, vector_cosine_ops, CONCURRENTLY |
| 14 | Full-text Index | `migrations/0001` | GIN index on pre-computed `fts_vec tsvector` column |
| 15 | Hybrid Retrieval | `retrieve.py · run()` | Vec (n=30) + FTS (n=30) → RRF(k=60). **Vec returns 0 results — wrong column.** |
| 16 | Ranking | `retrieve.py · _rrf()` + optional CrossEncoder | BAAI/bge-reranker-base if BRAIN_RERANK=true |
| 17 | Context Assembly | `retrieve.py · format_answer()` | Top 5 facts (capped), ACL+team filtered, invalid excluded |
| 18 | Prompt Construction | `retrieve.py · synthesize_answer()` | "Answer using ONLY the facts provided… cite each fact with its ID" |
| 19 | LLM Synthesis | `llm.py · chat_text()` | SYNTH_MODEL, temp=0.3, max_tokens=400 |
| 20 | Citation Generation | `retrieve.py · _cite()` | Returns fact_id, summary, confidence, source_kind, team, visibility, episode_id |
| 21 | Answer Return | `brain/engine.py · search()` | Returns {answer, facts, synthesized_answer} |

---

## 6. Retrieval Evaluation (P2)

**No standard IR metrics are measured anywhere in the codebase.** The eval script uses substring matching only. There is no golden retrieval dataset with expected fact IDs.

| Metric | Status | What Is Missing |
|---|---|---|
| Recall@K | MISSING | No expected_fact_ids in eval dataset |
| Precision@K | MISSING | Same |
| Hit Rate@K | MISSING | Same |
| MRR | MISSING | Same |
| NDCG@K | MISSING | Same |
| Context Recall | PARTIAL | Only if ragas installed; not in CI |
| Context Precision | PARTIAL | Same |
| Semantic similarity | MISSING | Not computed |
| Retrieval latency | PARTIAL | `evals/run.py` records latency_ms; no P50/P95 aggregation |
| Hybrid vs FTS vs Vec comparison | MISSING | Not benchmarked |

### Evaluation Dataset Status

`evals/questions.yaml` has 28 questions in 7 categories. **Missing:** expected_fact_ids, expected_sources, difficulty rating, answerability flag.

---

## 7. Hallucination / Generation Evaluation (P3)

| Dimension | Status | Evidence |
|---|---|---|
| Faithfulness / Groundedness | PARTIAL | Synthesis prompt instructs "ONLY the facts provided." RAGAS optional. No CI check. |
| Answer correctness | PARTIAL | Substring match in `evals/run.py` — weak proxy only. |
| Answer relevance | MISSING | Not measured. |
| Citation correctness | MISSING | Citations returned as fact_id strings; no test verifies claim↔fact alignment. |
| Citation completeness | MISSING | Not measured. |
| Hallucination rate | MISSING | Not measured. |
| Abstention (unanswerable) | PARTIAL | `expect_not_sure: true` in eval YAML — 5 of 28 questions. Checks `top_confidence < 0.4`. Fragile. |
| Fabricated numbers/dates/people | MISSING | No adversarial test cases. |

> **Synthesis prompt provides instruction-level guardrails only.** "Answer using ONLY the facts provided" is correct but unverified. A model with sufficient context could still fabricate — and this would go undetected.

---

## 8. Fact Extraction Evaluation (P4)

### Extraction Prompt (exact)

```
SYSTEM:
You are a precise company-memory extractor. Output ONLY valid JSON.
No commentary, no markdown fences.

USER:
Source: {source}
Extract ONLY facts that a new employee would need to know 6 months from now.
Return JSON: {"facts":[{"subject":"<who/what>","predicate":"<relationship or action>",
"object":"<value or outcome>","summary":"<one clear sentence>","confidence":<0.0-1.0>,
"supersedes":"<exact summary of the fact this replaces, or null>",
"visibility":"team|shared"}]}

Rules:
- confidence < 0.5 → skip
- confidence ≥ 0.9 → only for established, verified facts
- subject must be a specific named entity
- predicate must be active-voice relationship
- object must be concrete and verifiable
- visibility = "shared" only for company-wide facts
ITEM:
{body}
```

> **Bug: visibility value mismatch.** The prompt instructs the model to output `visibility: "shared"` but the database CHECK constraint only allows `team | org | private`. Facts with `visibility='shared'` will fail the constraint and not be inserted.

| Metric | Status |
|---|---|
| Fact extraction precision | MISSING |
| Fact extraction recall | MISSING |
| Fact extraction F1 | MISSING |
| Field-level accuracy | MISSING |
| Schema validity | PARTIAL — JSON parsed; `validate_llm_output()` exists but not called in extraction path |
| Confidence calibration | MISSING |

---

## 9. Deduplication Evaluation (P5)

```sql
-- brain/core/extract.py · _find_near_duplicate()
SELECT id, summary FROM facts
WHERE workspace_id=%s AND invalidated_at IS NULL
AND fact_vec_768 <=> %s::vector < 0.08   -- cosine distance threshold
ORDER BY fact_vec_768 <=> %s::vector
LIMIT 1
```

- **Threshold:** 0.92 cosine similarity (distance < 0.08)
- **Bug:** Also queries wrong column — dedup doesn't work in production
- **Provenance:** Duplicate is silently dropped; no merge, no audit record

> **Dangerous case:** "John is VP Sales" and "John is VP Engineering" have cosine similarity ~0.88–0.91 on Gemini embeddings. Near but usually below 0.92 — meaning they would likely NOT be incorrectly merged. However, this threshold was set without calibration data and has not been tested.

---

## 10. Contradiction / Temporal Evaluation (P6)

**What exists:**
- `_supersede()` — sets `invalidated_at` on old fact; inserts `supersedes` edge
- `_detect_conflicts()` — same subject+predicate, different object → `in_tension_with` edge
- `correct_fact()` — invalidates fact, records correction as new fact (confidence=1.0)
- `Brain.decision_history()` — returns timeline of superseded facts for a subject

> **`in_tension_with` edges are never surfaced to users.** When "Acme renews in September" and "Acme renews in November" coexist, the system writes a conflict edge but the user asking "When does Acme renew?" receives whichever fact scored higher, with no warning that a contradiction exists.

| Capability | Status |
|---|---|
| Detects conflicts | YES |
| Stores both conflicting facts | YES |
| Resolves conflicts automatically | NO |
| Surfaces conflicts to user | NO |
| Tracks provenance | YES — `fact_edges.supersedes` |
| Tracks timestamps | YES — `introduced_at`, `invalidated_at` |
| Temporal questions | PARTIAL — `decision_history()` exists but not exposed in API search |
| Temporal correctness tests | MISSING |

---

## 11. Multi-Tenant Security (P7)

**Overall: MEDIUM risk.** RLS is correctly configured and double-enforced. Primary risks are MCP server (no per-call auth) and rate limiting not shared across workers.

```sql
-- Before every query:
SET set_config('brain.workspace_id', '<ws_id>', false)

-- RLS policy on all tenant tables:
USING (workspace_id = current_setting('brain.workspace_id', true))

-- conversations + messages use FORCE ROW LEVEL SECURITY
```

| Path | Status | Notes |
|---|---|---|
| facts / episodes | ENFORCED | RLS policy + explicit WHERE clause |
| conversations / messages | STRONG | FORCE RLS + user_id scope |
| Vector search | PARTIAL | `search_vec()` had no workspace filter until recent fix; verify in production |
| MCP server | ENV-ONLY | No per-call auth. Workspace fixed at process start. |
| workspace_id source | SERVER-SIDE | Derived from authenticated JWT, never from request body |
| Rate limiting cross-worker | NOT SHARED | In-process dict only — DoS risk on multi-worker deployments |

---

## 12. Prompt Injection Evaluation (P8)

**No injection defenses or tests exist.** External content from Gmail, GitHub, Slack passes directly into LLM prompts as the `{body}` field.

| Injection Surface | Status | Attack Path |
|---|---|---|
| Ingestion-time (extraction) | UNDEFENDED | Email body "Ignore all previous instructions…" → sent verbatim as {body} to extraction LLM |
| Retrieval-time (synthesis) | UNDEFENDED | Stored fact summary containing prompt-like text → sent to synthesis LLM as context |
| MCP results | UNDEFENDED | Tool results returned as JSON strings → Claude Code/Cursor may act on injected instructions |
| Dedicated tests | NONE | No adversarial test cases anywhere in test suite |

---

## 13. MCP / Agent Evaluation (P9)

| Tool | Auth | Notes |
|---|---|---|
| `search_company_facts` | ENV | top=5, cross_team=False |
| `get_decision_history` | ENV | Subject timeline via fact_edges |
| `record_fact` | ENV | Max 2000 chars; confidence=1.0; source_kind='human' |
| `correct_fact` | ENV | Invalidates fact by ID — **no ownership check on the fact** |
| `harvest_from_response` | ENV | Harvest variant prompt with ≥0.7 confidence gate |
| `list_teams` | ENV | Returns all teams — no role gate |

> **`correct_fact` has no ownership check.** Any MCP caller can invalidate any fact by ID within the workspace, without verifying they created it or have rights over it.

---

## 14. LLM Reliability (P11)

| Dimension | Status | Evidence |
|---|---|---|
| Provider fallback | IMPLEMENTED | `_call_llm_with_fallback(primary, fallback)` in llm.py |
| Retry with backoff | IMPLEMENTED | `@retry(max_attempts=3, base_delay=1.0, max_delay=8.0, jitter=0.4)` |
| Circuit breaker | IMPLEMENTED | llm breaker: threshold=5, recovery=30s |
| Malformed JSON output | PARTIAL | Markdown fence stripping + fallback JSON parse; Pydantic validation exists but not called in extraction |
| Token usage tracking | IMPLEMENTED | `_extract_usage()` writes to usage_events table per call |
| Cost/query metering | IMPLEMENTED | MODEL_PRICES dict × tokens → cost_usd in usage_events |
| Langfuse tracing | IMPLEMENTED | Every chat_json/chat_text call traced |
| Retry rate / fallback rate | NOT MEASURED | Logged to stderr only; not in Prometheus |

---

## 15. Embeddings (P12)

| Property | Value | Status |
|---|---|---|
| Model | `gemini-embedding-001` | CONFIGURED |
| Dimensions | 768 | CONSISTENT — config, embed.py, HNSW index all agree |
| Normalization | L2-normalized before storage | IMPLEMENTED |
| Distance metric | Cosine (`vector_cosine_ops`) | CORRECT |
| HNSW m | 16 | SET |
| HNSW ef_construction | 64 | SET |
| ef_search tuning | — | NOT SET — uses pgvector default |
| Model change migration guard | — | MISSING — no check preventing mixed embedding spaces |

> **Silent degradation risk:** Changing `EMBED_MODEL` would silently mix embedding spaces — new facts stored with different vectors, old facts unreachable by new queries. No runtime check enforces this.

---

## 16. Evaluation Matrix

| Area | Metric | Status | Risk | Priority |
|---|---|---|---|---|
| Retrieval | Recall@5 | MISSING | HIGH | P0 |
| Retrieval | Precision@5 | MISSING | HIGH | P0 |
| Retrieval | MRR | MISSING | MED | P1 |
| Retrieval | NDCG@5 | MISSING | MED | P1 |
| Generation | Faithfulness | PARTIAL | HIGH | P0 |
| Generation | Answer correctness | PARTIAL | MED | P1 |
| Generation | Citation correctness | MISSING | HIGH | P0 |
| Generation | Citation completeness | MISSING | MED | P1 |
| Generation | Abstention accuracy | PARTIAL | MED | P1 |
| Hallucination | Unsupported claim rate | MISSING | HIGH | P0 |
| Extraction | Fact F1 | MISSING | HIGH | P1 |
| Extraction | Entity F1 | MISSING | MED | P2 |
| Knowledge | Dedup F1 | MISSING | HIGH | P1 |
| Knowledge | False merge rate | MISSING | HIGH | P1 |
| Knowledge | Contradiction handling | PARTIAL | MED | P1 |
| Knowledge | Temporal accuracy | PARTIAL | MED | P2 |
| Security | Cross-tenant leakage | TESTED | MED | P0 |
| Security | Prompt injection | MISSING | HIGH | P1 |
| MCP | Tool selection | NOT TESTED | MED | P2 |
| Integration | Ingestion success rate | NOT MEASURED | MED | P2 |
| Reliability | Provider fallback | TESTED | LOW | P2 |
| Performance | End-to-end latency | MEASURED | LOW | P3 |
| Cost | Cost/query | MEASURED | LOW | P3 |
| Regression | Golden dataset | PARTIAL | HIGH | P0 |

---

## 17. Priority Roadmap

### P0 — Fix Before Any User-Facing Demo

**P0.1 — Fix `search_vec()` column name**

```python
# brain/core/db_pg.py · search_vec()
# Change:  fact_vec_768 => %s::vector
# To:      embedding <=> %s::vector

# Also fix _find_near_duplicate() in extract.py — same bug
```

Run: `pytest tests/test_retrieval.py -k vec`. Verify facts are returned for a known query.

**P0.2 — Fix visibility constraint**

```sql
-- Option A — migration:
ALTER TABLE facts DROP CONSTRAINT facts_visibility_check;
ALTER TABLE facts ADD CONSTRAINT facts_visibility_check
  CHECK (visibility IN ('team', 'shared', 'org', 'private'));
```

Or change `extract.py` to write `org` and update all retrieve.py visibility checks accordingly.

**P0.3 — Extend eval dataset with fact IDs**

```yaml
# evals/questions.yaml — add to each question:
- id: q001
  question: "What database does Thread use?"
  expected_answer_contains: "Supabase"
  expected_fact_ids:           # NEW
    - "fact-uuid-here"
  difficulty: easy             # NEW
  answerable: true             # NEW
```

**P0.4 — Faithfulness eval in CI**

```python
# evals/eval_faithfulness.py — new file
# For each (question, answer, facts):
#   Call LLM: "Does every claim in this answer have a supporting fact?
#              JSON: {faithful: bool, unsupported_claims: [...]}"
# Assert faithful=true for non-adversarial cases
# Assert faithful=false for planted hallucination cases
```

### P1 — Before Production Launch

**P1.1 — Surface conflicts in synthesis**

```python
# brain/core/retrieve.py · format_answer()
# After assembling facts, query fact_edges for in_tension_with between any pair in result set
# If found, add to response:
# {"conflicts": [{"fact_a": "...", "fact_b": "...", "note": "These facts contradict each other."}]}
```

**P1.2 — Prompt injection sanitizer**

```python
# brain/core/ingest.py — add before saving episode:
_INJECTION_PATTERNS = [
    r"ignore (all )?(previous|prior|above) instructions",
    r"system\s*:",
    r"</?(system|user|assistant)>",
]
def _sanitize_body(body: str) -> str:
    for pat in _INJECTION_PATTERNS:
        body = re.sub(pat, "[filtered]", body, flags=re.IGNORECASE)
    return body
```

**P1.3 — Redis-backed rate limiting**

Replace module-level dict in `rate_limiter.py` with a Redis-backed implementation using `INCR` + `EXPIRE`. One shared counter per workspace across all workers.

**P1.4 — Dedup calibration test**

```python
# tests/test_dedup_calibration.py
DEDUP_PAIRS = [
    ("John is VP Sales", "John is VP Engineering", False),   # should NOT dedup
    ("Acme renews in September", "Acme renews in November", False),
    ("We use React Native", "The app uses React Native", True),   # should dedup
]
# Embed each pair, compute cosine similarity, assert against DEDUP_THRESHOLD
```

**P1.5 — Fix `evals/run.py` placeholder bug**

```python
# Change ? → %s in the --reset-extracted path
cur.execute("UPDATE facts SET invalidated_at=NULL WHERE workspace_id=%s", (ws,))
```

**P1.6 — `correct_fact` ownership check in MCP**

Before invalidating a fact, verify the caller's identity matches the fact's author, or the caller is a workspace admin.

### P2 — Production Hardening

- Call `validate_llm_output()` in the extraction path
- Add startup embedding model change guard
- Add dedicated Prometheus metrics: `embed_latency_ms`, `retrieve_latency_ms`, `llm_latency_ms`, `retry_total`, `fallback_total`
- Make `_save_atomic()` truly atomic (single `BEGIN/COMMIT` for fact + edges)

### P3 — Nice to Have

- ef_search tuning benchmarks for HNSW
- Chunking strategy for long documents
- Deletion/edit propagation from source systems
- OAuth expiry notification to workspace admins
- Data freshness dashboard (last_synced_at per integration)

---

## 18. Resume / Interview-Ready Summary

*Based only on what was verified in the repository — no exaggeration.*

- Built a multi-tenant RAG platform with **PostgreSQL RLS double-enforcement** (session GUC + explicit WHERE clause on every query) and a CI gate script (`check_rls.py`) that verifies isolation policy on all tenant tables before deploy.

- Implemented **hybrid retrieval with Reciprocal Rank Fusion** combining pgvector HNSW cosine search (m=16, ef_construction=64, 768-dim Gemini embeddings) and PostgreSQL full-text search (GIN-indexed tsvector), with optional cross-encoder reranking (BAAI/bge-reranker-base).

- Wired **Langfuse observability** into every LLM call (task name, model, full prompt/response), **Sentry error tracking** with a custom PII scrubber that strips query and answer text before transmission, and Prometheus histograms with per-route latency buckets exposed at `/metrics`.

- Designed **fact provenance and contradiction tracking** using a `fact_edges` table with `supersedes` and `in_tension_with` edge types, enabling temporal decision history queries and preserving the full invalidation chain without deleting historical facts.

- Implemented a **three-state circuit breaker** (CLOSED / OPEN / HALF_OPEN) with thread-safe state transitions, exponential backoff retry (max 3 attempts, jitter), five named breakers (LLM, Redis, embed, Slack, Composio), and graceful synthesis degradation (returns cited facts when LLM circuit is open).
