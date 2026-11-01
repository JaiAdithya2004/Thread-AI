_1 runs, 2026-10-22, commit `8d0458e`. Mean over runs; range in parentheses; counts are pooled across runs._

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
