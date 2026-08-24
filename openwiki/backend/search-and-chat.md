---
type: domain_page
title: Search and RAG chat
description: Local SQL search, parallel multi-source external search (OpenAlex + Semantic Scholar + arXiv) with RRF merging and spell correction, pgvector semantic search, the /import resolution flow, and the per-paper SSE RAG chat.
tags: [search, rrf, semantic-search, pgvector, chat, rag, sse]
---

# Search and RAG chat

`carrel/api/search.py` is the largest router in the app. It fans out across
the local library and three external metadata sources, merges results with
deterministic field-authority rules, optionally applies spell correction,
and exposes a separate semantic endpoint over embedded chunks. `carrel/api/chat.py`
builds on the same embedding primitives to provide a per-paper RAG chat
streamed over Server-Sent Events.

## Search endpoints

| Endpoint | Purpose |
|---|---|
| `GET /search/local` | SQL ILIKE over library papers only. |
| `GET /search/external` | Parallel fan-out to OpenAlex + Semantic Scholar + arXiv, merged/deduped, with library membership resolved. |
| `GET /search` | Combines both; default surface used by the Search page. |
| `GET /search/semantic` | pgvector cosine search over embedded chunks, grouped by paper. |
| `POST /import` | Resolve an OA id / DOI / arXiv id / S2 id to importable metadata and add to the library. |

All four GET endpoints accept `q`, `limit`, and `correct` (default true;
applies SymSpell correction seeded from the local library — see
`carrel/spelling.py`). The external/combined endpoints additionally accept
`year_from`, `year_to`, `min_citations`, `open_access_only`, `sort`
(`relevance` | `citations` | `date`), and `sources` (allow-list of
`openalex`, `semantic_scholar`, `arxiv`).

## Local search

`_local_search_items` issues one ILIKE query over in-library papers matching
on title, abstract, notes, authors (cast to string), DOI, and arXiv id,
ordered by `updated_at desc`. It builds a `MutableSearchHit` tagged with
`SOURCE_LIBRARY` so the merge layer can identify it.

## Multi-source external search

`_multi_source_search(q, filters, per_source_limit)` runs the three source
adapters concurrently in a `ThreadPoolExecutor(max_workers=3)`; the local
ILIKE query runs separately in `_local_search_items` and is merged into
the same list on the combined endpoint. Each external adapter assigns
**1-based per-source ranks** (`hit.ranks[source] = i + 1`) before merge;
the local hit gets a synthetic rank of 1 on the combined endpoint (see
below).

- `openalex_client.search_works` (faceted Works search, abstract inverted-index
  reconstruction, Zenodo filtering, OA PDF candidates).
- `semanticscholar_client.search_papers` (Graph API relevance or bulk
  citation/date sort, contributes citation count, venue type, TLDR).
- `arxiv.fetch_recent` / search adapter (Atom API, contributes canonical
  arXiv PDF link and freshest preprint metadata).

Each adapter returns `MutableSearchHit` records. Per-source exceptions are
caught and surfaced as strings in `warnings` rather than failing the whole
request.

The three lists are merged by `merge_mod.merge_search_hits`:

1. Dedup keys tried in order: DOI, arXiv id, S2 paperId, OpenAlex W-id,
   then a normalized title as last resort.
2. Field authority on collision:
   - `citation_count`: max across sources.
   - `venue` / `venue_type`: S2 wins, then OA, then arXiv.
   - `authors`: first non-empty, preferring OpenAlex (ids + affiliation).
   - `abstract`: first non-empty.
   - `pdf_url`: arXiv PDF wins (canonical, never a landing page), then OA,
     then S2.
   - `tldr`: S2 only.
   - Identifiers: union; never drop an id a source contributed.

### Sorting

- `relevance` uses reciprocal-rank fusion (`merge_mod.reciprocal_rank_fusion`).
  For the combined endpoint, library hits receive a strong rank-1 head start
  when they have no external rank, so papers the user already owns surface
  first.
- `citations` sorts by `citation_count desc` (nulls last).
- `date` sorts by `publication_date desc`.

After sorting, `_resolve_library_membership` does a batched DOI / arXiv /
S2 / OpenAlex-id lookup against the `papers` table and stamps
`in_library`/`library_id`/`status` on external-only rows.

## Spell correction

`carrel/spelling.py` wraps SymSpell with:

- The bundled 82k English frequency dictionary.
- A supplement seeded once per process from local library titles and
  abstracts, so domain jargon (`BERT`, `RAG`, `arxiv`) is never "corrected".
- Identifier passthrough for DOIs, arXiv ids, URLs, and numeric tokens.
- A `_PROTECTED` set of common CS terms that must never be rewritten.
- CJK detection that bails out before SymSpell mangles non-Latin queries.

Seeding is lazy (first search pays ~100ms) and guarded by `_seed_lock`.

## Semantic search

`GET /search/semantic` runs `_semantic_search(session, q, limit)`:

1. Embed the query with `embeddings.embed_texts([q], model=cfg.embeddings.model, batch_size=1)`.
2. Fetch `raw_limit = min(limit * 3, 100)` chunks so grouping by paper has
   candidates to trim. On Postgres, `_semantic_search_postgres` issues
   `Chunk.embedding.cosine_distance(q_vec)` ordered asc with that raw limit
   (uses the `ix_chunks_embedding_hnsw` HNSW index when it built
   successfully; falls back to a sequential scan otherwise).
3. On SQLite, `_semantic_search_sqlite` decodes the JSON-stored vectors and
   computes cosine similarity in pure Python over all chunks (test path;
   not production).
4. Group chunk hits by paper, keep the top three chunks per paper as
   `SemanticSearchHit` rows with heading/snippet/score, and return papers
   ordered by best chunk score (capped at `limit`).

Snippets are centered on the lowercased query when present; otherwise the
first 280 chars are returned.

## Import (`POST /import`)

`_resolve_work_for_import` tries, in order:

1. OpenAlex W-id (the last path segment of the provided id).
2. DOI via `openalex_client.lookup_by_doi`.
3. arXiv id via `lookup_by_arxiv_id` (with a title-hint fallback).
4. If none of those resolved and an S2 id is available (or derivable from
   DOI/arXiv), `semanticscholar_client.fetch_paper`.
5. If S2 returned a DOI/arXiv/title, retry OpenAlex by DOI → arXiv → strict
   title similarity (≥ 0.85).
6. If OpenAlex still has nothing but S2 returned the paper, build a synthetic
   Work-shaped dict with `_source="semanticscholar"` so the paper can still be
   imported with an S2-derived identity.

`import_external_paper` then normalizes the record, inserts it as a new
`Paper` with `in_library=True`, and returns its id; if a paper with the same
canonical id already exists (including via cross-id dedup), it is refreshed
and returned instead.

## RAG chat (`POST /papers/{id}/chat`)

`carrel/api/chat.py` streams an LLM answer over SSE
(`media_type="text/event-stream"`, headers `Cache-Control: no-cache` and
`X-Accel-Buffering: no` so proxies do not buffer tokens). The endpoint
returns HTTP 409 when `paper.md_path` is unset **or** when the parsed
markdown file is missing on disk, and 404 when the paper does not exist.

Request flow:

1. `ChatRequest.messages` must contain at least one turn. The last user
   message is the question.
2. `_retrieve_chunks(session, paper, query, top_k)` loads all `Chunk`
   rows for the paper, embeds the query, and ranks them:
   - Postgres: `_rank_postgres` — `Chunk.embedding.cosine_distance`
     ordered asc, limit `top_k` (default `cfg.llm.rag_top_k = 6`).
   - SQLite: `_rank_sqlite` — in-memory cosine over decoded vectors.
   - If embedding raises, no chunks exist, **or ranking returns no
     hits**, it falls back to the truncated parsed Markdown via
     `summarize._prepare_body(body, cfg.llm.chat_fulltext_chars)`
     (default 24 000 chars), labeling sources as
     `["full text (truncated)"]`. The fallback also requires
     `md_path` and a file on disk (same 409 contract).
3. `_build_messages(paper, context_block, history, history_limit)`
   produces the message list:
   - a fixed **Chinese** system prompt that requires the model to
     answer in the question's language, cite section headings, refuse
     to hallucinate, and wrap math in `$...$` / `$$...$$` for KaTeX;
   - a single `<paper-context>` user message containing the title,
     authors, and the retrieved context block, ending with
     "依据以上论文片段回答用户的问题。";
   - the client's prior turns, with `system` turns dropped and the
     list trimmed to the most recent `cfg.llm.chat_history_limit`
     (default 6) non-system turns.
4. The streaming generator yields frames in this exact order, each
   wrapped as `data: <json>\n\n` by `_sse` / `_event`:
   - first `{"sources": [...]}` listing the chunk headings that
     informed the answer (or the full-text fallback label);
   - zero or more `{"t": "<delta>"}` token frames produced by
     `llm.chat_stream(messages, model=..., fallback_model=...,
     temperature=..., timeout=...)` — the streaming sibling of
     `llm.chat_json` with the same provider-prefix key resolution and
     primary-then-fallback attempt chain (model is
     `cfg.llm.chat_model or cfg.llm.summarize_model`; fallback is
     `cfg.llm.chat_fallback_model or cfg.llm.fallback_model`);
   - on exception, a terminal `{"error": "..."}` frame;
   - on success, the literal terminal sentinel `[DONE]` (not JSON).

The transcript is persisted separately via
`PUT /papers/{id}/chat/messages` (whole-document replace, like notes) and
retrieved with `GET .../messages`. Rows are stored in `chat_messages`
ordered by id; `chat_history_limit` (default 6) trims prior turns before
sending them to the model.

## Pydantic shapes

- `SearchResultItem`, `SearchResponse` (`query`, `corrected_from`,
  `results`, `warnings`).
- `SemanticSearchHit`, `SemanticSearchResult`, `SemanticSearchResponse`.
- `ImportPaperIn`, `ImportPaperOut`.
- `ChatTurn`, `ChatMessageOut`, `ChatMessagesIn/Out`.

## Focused tests

- `tests/test_search_api.py` — local/external/combined endpoints, filters,
  sorting, library membership resolution, /import resolution.
- `tests/test_search_merge.py` — pure merge field-authority and RRF rules.
- `tests/test_search_semantic.py` — pgvector/SQLite ranking and grouping.
- `tests/test_chat_history_api.py` — PUT/GET transcript replacement.
- `tests/test_spelling.py` — identifier passthrough, protected terms, CJK
  bail-out.
- `tests/test_s2_client.py`, `tests/test_openalex_client.py`,
  `tests/test_arxiv_search.py`, `tests/test_normalize.py` cover the source
  adapters and normalization.

## Validation

```bash
.venv/bin/python -m pytest tests/test_search_api.py tests/test_search_merge.py tests/test_search_semantic.py tests/test_chat_history_api.py tests/test_spelling.py -q
```

The semantic endpoint requires a real embedding API key; tests cover ranking
with deterministic SQLite vectors, and the live providers are exercised
manually against a configured `.env`.

## Evidence

- Search: `carrel/api/search.py`.
- Merge and RRF: `carrel/sources/merge.py`.
- Spell correction: `carrel/spelling.py`.
- Chat: `carrel/api/chat.py`.
- Embeddings: `carrel/embeddings.py` (see
  [../enrichment/embeddings.md](../enrichment/embeddings.md)).
- Frontend: `frontend/src/pages/Search.tsx`,
  `frontend/src/components/PaperChat.tsx`.
