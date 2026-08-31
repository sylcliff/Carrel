# Knowledge-Graph RAG (HippoRAG) in Carrel

> Status: **design draft** — not implemented. Captures decisions from the
> 2026-08-29 spike (LightRAG vs HippoRAG quickstart on 3 Carrel papers).

## Why HippoRAG, not LightRAG

Both are pure-Python GraphRAG packages, but the cost/perf profile diverges
sharply enough to pick one and ship:

| | LightRAG 1.5.7 | HippoRAG 2.0.0a3 |
|---|---|---|
| Insert 3 papers (~670K chars) | **76+ min, all 3 docs timed out** at default 480s worker_timeout | **55.8s** ✅ |
| Insert LLM calls | ~200 chunks × 2-3 calls = **~600** | 3 docs × 2 calls = **6** |
| Insert cost (Claude-grade LLM) | ~**$10** | ~**$0.05** |
| Query (retrieve + LLM answer) | n/a (didn't reach query) | 2s retrieve + 31s answer |
| Graph size on 3 papers | 0 (failed) | 170 phrase nodes + 3 passage nodes, 500 edges |
| Retrieval algorithm | dual-layer (local + global) | Personalized PageRank over igraph |

The deciding factor: **Carrel indexes incrementally as the user adds papers
to the library.** LightRAG's per-chunk LLM extraction makes the per-paper
indexing cost 200× higher. We re-index whenever a paper is re-parsed, so
LightRAG's cost compounds.

HippoRAG is good enough: the spike's LLM-generated answer about
fault-tolerant quantum computation was coherent, grounded, and admitted
when the corpus was thin (it does not hallucinate coverage).

## Goal

Add a **cross-paper knowledge graph retrieval layer** that complements
Carrel's existing per-paper RAG (paper chat, semantic search) and full-text
search. It surfaces the **passages most related to a query across the whole
library** and explains *why* via the graph.

## Non-goals

- Not a replacement for paper chat (per-paper, conversational).
- Not a replacement for the existing semantic search (vector-only).
- Not a community/clustering discovery view (LightRAG-style global mode).
- Not a self-updating graph — we re-index on parse/re-parse, not on every
  metadata change.

## Where it lives

```
carrel/
├── rag.py                  # HippoRAG singleton + add_paper/rag_search
├── rag_jobs.py             # Background index jobs (per paper, similar to citations refresh)
└── api/
    └── rag.py              # /api/rag/search, /api/rag/stats, /api/rag/reindex

data/
└── hipporag/               # save_dir (graph.pickle + parquet + llm_cache)
    └── {llm}_{embed}/      # one workspace per backend pair, like LightRAG

docs/knowledge-graph-rag.md # this file
```

The `HippoRAG` object is a process-wide singleton, lazy-instantiated on
first use. **No separate server, no Neo4j, no Postgres** — persistence is
just `pickle` + `parquet` in `data/hipporag/`, same shape as the spike.

## Backend choices

The spike proved the following config works end-to-end against Carrel's
existing Volcano Ark key:

```python
BaseConfig(
    save_dir="./data/hipporag/{llm}_{embed}",
    llm_name="ark-code-latest",                                  # Volcano plan tier
    llm_base_url="https://ark.cn-beijing.volces.com/api/plan/v3/",  # Bearer-authenticated
    embedding_model_name="facebook/contriever-msmarco",          # 768d, 440MB, local
)
```

Alternates we considered:
- **Embedding**: `text-embedding-3-small` (1536d, requires OpenAI key);
  `BAAI/bge-small-en-v1.5` (384d, ~140MB on disk) — both are smaller than
  contriever but unsupported by HippoRAG v2's `_get_embedding_model_class`
  dispatcher (only NV-Embed-v2 / GritLM / contriever / text-embedding match).
  We can subclass the dispatcher if a smaller model is needed.
- **LLM**: any OpenAI-compatible endpoint. The current key works on
  `/api/plan/v3` (chat) but the embedding endpoint on the same host does
  not — that's why embedding is local.

## Indexing flow

```text
user imports / parses paper
        │
        ▼
papers.in_library = True      ← existing logic
        │
        ▼
rag_jobs.enqueue(paper_id)    ← new background job
        │
        ▼
rag.add_paper(paper_id, text) ← OpenIE (2 LLM calls) + embed (local)
        │
        ▼
graph + embeddings persisted to data/hipporag/
```

**Triggers** (all go through `rag_jobs.enqueue`):

1. **Import**: paper transitions to `in_library=True` for the first time
   (the existing `sync` flow per [[sync-discovers-inbox]]; piggyback on
   the same "first time imported" event).
2. **Re-parse**: MinerU re-parse updates `paper.md`. Old doc fingerprint
   (sha256 of body) is stored alongside the paper; mismatch → enqueue.
3. **Manual**: `POST /api/rag/reindex` (admin/ops).

The job is **idempotent** — HippoRAG's `force_openie_from_scratch` is off
by default, and we keep a `(paper_id, body_hash)` map to skip no-ops.

## Query flow

```text
GET /api/rag/search?q=fault-tolerant+magic+state&top_k=5
        │
        ▼
rag.search(query, top_k) → [{doc, paper_id?, score}, ...]
        │
        ▼
hydrate paper_id from doc prefix (OpenIE adds a passage marker)
        │
        ▼
return [{paper_id, snippet, score, graph_path: [entity, relation, ...]}, ...]
```

The `paper_id` join is a single SQL `SELECT` keyed off an in-doc marker
that HippoRAG embeds when we wrap each document with a sentinel
(`"<paper:{id}>" + text + "</paper:{id}>"`). Cheap and stable.

The graph_path comes from the PPR node sequence we already have to compute
for retrieval — we can persist the last 3-5 phrase nodes per query for
display purposes ("how did the system find this?").

## Cost & latency budget

Measured in the spike on a 3-paper / 670K-char corpus:

| Phase | Wall-clock | LLM calls | Embedding |
|---|---|---|---|
| **First-time index** | 55.8s | 6 | ~30s of local contriever |
| **Re-index** (LLM cache hit) | <5s | 0 | 0 (parquet reused) |
| **Query** retrieve only | 2s | 0 | 0 |
| **Query** retrieve + LLM answer | 33s | 1 | 0 |

Scaling extrapolation (rough, NOT measured):
- **10× more papers** (30): re-index of new ones ~3-5 min; full rebuild ~hours.
  Acceptable because rebuild is rare.
- **Query latency** scales with graph size; igraph PPR is roughly O(E) per
  query, so 5000 edges = 100× slower than 50. We'll cap retrieval at
  `retrieval_top_k=200` and monitor.

LLM spend is the binding constraint. Worst case (rebuild entire library
monthly): ~$0.05 × (papers / 3) = $0.50 / month at 30 papers.

## Failure modes & roll-out

1. **LLM endpoint down / auth expired** — `rag_jobs` retries with
   exponential backoff, then marks the paper as `rag_status="failed"` in
   the DB. Library still works (semantic search, paper chat unaffected).
2. **Embedding model changes** (HF hub moves / different HF mirror) —
   `data/hipporag/{llm}_{embed}/` is keyed on both, so a new model =
   new directory, no overwrite.
3. **Cost overrun** — monthly LLM spend on `rag_jobs` is logged to
   `usage` (Carrel's existing usage-tracking table). Add a soft cap;
   beyond it, the job soft-fails and logs a warning.
4. **Quality regression** — feature-flagged behind `RAG_GRAPH_ENABLED=true`
   env (default off until QA'd on the real library). Kill switch is a
   single env flip.

## Integration with existing Carrel surfaces

- **Paper chat** ([[paper-chat]]) — unchanged. The new `/api/rag/search` is
  a *cross-paper* search; paper chat is *per-paper*. The two can
  complement each other: future feature: paper chat could optionally pull
  in 2-3 cross-paper passages from HippoRAG as additional context.
- **Search** ([[multi-source-search]]) — `/api/search` returns papers;
  `/api/rag/search` returns passages. Both can be queried from the same
  search bar; UI tab switch.
- **MCP** ([[mcp-architecture]]) — if useful, expose `rag_search` as an
  MCP tool for external agents. Defer until someone asks.
- **Topics** ([[topics-classification]]) — orthogonal. Topics classify
  whole papers; HippoRAG finds passages. Could feed `Topic.suggested_papers`
  with passage-level hits in the future.

## Open questions

1. **Granularity of `paper_id` join** — do we accept the sentinel-tag
   trick, or do we rebuild the graph with explicit `passage_id → paper_id`
   edges? (sentinel = zero schema change; explicit = cleaner but a fork
   of HippoRAG).
2. **Should `rag_search` be available to anonymous users** (the library
   is local-only anyway), or only when `LIBRARY_SHARED=true`?
3. **Where in the frontend** — top nav? Inside the existing search
   dropdown? New `/library/graph` page?
4. **Re-index cadence** — manual only, or also a "weekly full rebuild"
   cron to catch corpus drift?

## Implementation order (when we green-light)

1. `carrel/rag.py` singleton + the spike's `quickstart.py` as `tests/rag_smoke.py`.
2. `carrel/rag_jobs.py` enqueue / retry / status, modeled on
   [[citations-s2]]'s refresh job.
3. Migration: add `rag_status` and `rag_indexed_at` columns to `papers`.
4. `GET /api/rag/search` and `POST /api/rag/reindex` routes.
5. Frontend: tab in `/library` search bar (lazy).
6. AGENTS.md update: per-endpoint cache/invalidation contract (mirrors
   [[react-query-frontend-l3]]).

## References

- Spike transcripts: `/tmp/kg_test_runs/{lightrag,hipporag}_workspace/`
- LightRAG web UI: `lightrag_webui` (we did *not* ship a comparable UI for
  HippoRAG — graph_view.html is a one-off debug aid).
- HippoRAG 2.0 paper / repo: `OSU-NLP-Group/HippoRAG` (NeurIPS'24).
