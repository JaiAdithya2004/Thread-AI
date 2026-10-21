_3 runs, 2026-10-21, commit `cc9e67c`. Mean over runs; range in parentheses; counts are pooled across runs._

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
