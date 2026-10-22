# Performance & Latency Guide

## Target

**Warm query p95 < 300 ms** (retrieval only, excluding LLM synthesis).

## Latency Budget Breakdown

| Step | Typical Latency | Notes |
|---|---|---|
| Embed query text | network-bound | Gemini `gemini-embedding-001` (768-dim) over HTTP; an in-process cache skips the round-trip for repeated queries |
| Redis cache lookup | ~1 ms | Full cache hit skips embed + DB entirely |
| Vector similarity (HNSW) | 10–30 ms | Scales with dataset size; HNSW avoids full scan |
| FTS search (GIN) | 5–20 ms | GIN index on `fts_vec`; scales logarithmically |
| RRF merge | <1 ms | Pure Python over two small lists |
| Candidate hydration | 1 round-trip | One `SELECT … WHERE id = ANY(…)` for all candidates (a per-fact version cost ~7 s for 37 candidates against a remote database) |
| LLM synthesis | 300–2000 ms | External API call; **excluded from retrieval budget** |

**Total retrieval (cache miss):** ~25–60 ms typical, <300 ms p95 goal.

## How to Measure

### Prometheus / /metrics endpoint
```bash
# After running a few searches, check p95:
curl http://localhost:7070/metrics | grep http_request_duration
```

### k6 load test
```bash
# Install k6 (https://k6.io/docs/get-started/installation/)
k6 run tests/load/k6_load_test.js

# With a custom base URL:
BASE_URL=http://localhost:7070 k6 run tests/load/k6_load_test.js
```

k6 prints a summary including `search_latency p(95)` after the test completes.

### Locust (Python alternative)
```bash
pip install locust
locust -f tests/load/locust_load_test.py --headless -u 20 -r 5 --run-time 2m \
    --host http://localhost:7070
```

### EXPLAIN ANALYZE
Run `scripts/explain_queries.py` to see actual query plans and execution times:
```bash
python scripts/explain_queries.py
```
The script exits with code 1 if any hot query uses a sequential scan on `facts`.

## Index Tuning

### HNSW (`ef_search`)
The `ef_search` parameter controls the search beam width at query time.
Higher = more accurate recall, but slower.

```sql
-- Check current setting (default is 40):
SHOW hnsw.ef_search;

-- Increase for higher recall (at cost of latency):
SET hnsw.ef_search = 100;
```

Only increase `ef_search` if recall testing shows it falling below 0.95.
For typical company knowledge bases (<100k facts), the default (40) is fine.

### `ef_construction` (index build time)
`ef_construction = 64` (set in `scripts/rebuild_index.py` and migration `0003`) governs index build quality.
Only rebuild the index with a higher value if you observe recall degradation
under the HNSW ef_search tuning above. Rebuilding requires:
```sql
DROP INDEX facts_vec_768_idx;
CREATE INDEX facts_vec_768_idx ON facts
    USING hnsw (fact_vec_768 vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
```

### GIN (`fts_vec`)
No tuning required. GIN is well-suited for tsvector and scales well to millions
of rows without parameter changes.

## Connection Pool Sizing

With PgBouncer in transaction mode:

| Concurrent users | Recommended `default_pool_size` |
|---|---|
| Up to 50 | 10 |
| Up to 100 | 20 (default) |
| Up to 500 | 40–60 |
| 500+ | Add read replicas |

At 20 pool connections and typical query latency of 30 ms, throughput is:
`20 connections × (1000 ms / 30 ms) ≈ 667 queries/sec` theoretical max.
Real-world headroom is ~50% of that due to variance, so ~300 qps before needing
to scale the pool.

## Redis Cache

The cache key is an MD5 of the query embedding vector. TTL is set by `CACHE_TTL`
in `brain/config.py` (default: 300 seconds).

A cache hit costs ~1 ms total and skips embed + both DB queries entirely.
Cache hit rate will be low for a diverse query set; high for repeated queries
(e.g. a team dashboard that re-runs the same searches).

## When to Scale Out

1. **Blocked on embedding**: embedding is a network call to Gemini, not local
   CPU, so more workers help only if requests are queueing behind the event
   loop. Check the in-process embed cache hit rate first — repeated queries
   should not be re-embedding at all.
2. **DB connection saturation**: increase `default_pool_size` in pgbouncer.ini,
   or switch to Supabase's managed pooler.
3. **p95 > 300 ms consistently**: profile with `scripts/explain_queries.py` to
   confirm indexes are being used. If yes, the bottleneck is likely the LLM
   synthesis step — move it to a background job or increase worker concurrency.
4. **> 1M facts in workspace**: consider partitioning `facts` by `workspace_id`
   and using `pgvector`'s IVFFlat index instead of HNSW for better scan parallelism.
