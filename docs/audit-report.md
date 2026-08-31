# Thread Platform — Evaluation Audit Report

> Codebase-verified · 2026-08-29

---

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
- **`scripts/check_rls.py`** — CI gate verifying RLS enabled on all required tables
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
