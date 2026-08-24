---
type: pipeline
title: Chunking and embeddings
description: Heading-aware Markdown chunker and the embed_paper pipeline that chunks parsed papers, embeds each chunk via litellm, and writes pgvector Vector(2048) / halfvec(2048) rows for semantic search.
tags: [chunking, embeddings, pgvector, hnsw, litellm, search]
---

# Chunking and embeddings

The chunker and embedder together turn a parsed `paper.md` into searchable
vectors. This is the `parsed → ready` step.

## Chunker — `carrel/chunking.py`

Splits ATX-headings Markdown into chunks of roughly `target_tokens`
words (default 900) with `overlap_tokens` (150) and a `min_tokens`
floor (200), while preserving the nearest preceding heading path as
context for each chunk.

- `estimate_tokens(text)` — heuristic `words / 0.75 ≈ tokens` (English
  and CJK; CJK characters each count as one "word"). A chunk-size knob,
  not a token counter; the embedding model re-tokenizes.
- `split_by_heading(md)` — walks ATX headings (`#`…`######`) maintaining
  a heading stack, yielding `(heading_path, body)` pairs. The path for a
  `### Methods` under `## Experiments` is `"Experiments / Methods"`. A
  pre-heading preamble becomes `("", body)`.
- `chunk_markdown(md, target_tokens, overlap_tokens, min_tokens)` —
  keeps a heading's body whole when it fits; otherwise splits it into
  sliding windows with overlap. Sub-`min_tokens` pieces merge into the
  next neighbor so no near-empty rows are stored. Returns a list of
  `Chunk(index, heading, content_md, token_count)`.

The implementation is a regex splitter by design (see the module
docstring): swapping in tiktoken/sentencepiece costs a dependency and a
cache for a tighter length target the embedding model does not care
about.

## Embedding helper — `carrel/embeddings.py`

Thin, lazy wrapper over `litellm.embedding`:

- Provider prefix → env-var map in `_KEY_ENV`:
  `volcengine → VOLCANO_API_KEY`, `deepseek → DEEPSEEK_API_KEY`,
  `openai → OPENAI_API_KEY`. `_key_for("volcengine/foo")` resolves the
  right key; secrets stay in `.env`, never in YAML.
- `embed_texts(texts, *, model, batch_size=50, max_retries=3, timeout,
  api_key=None)` — empty input returns `[]`; empty strings are skipped
  with a zero vector so callers don't have to pre-filter. Inputs are
  batched (default 50), each batch retried on 429/5xx with backoff.
- Vector dimension is whatever the model returns. The default model
  (`volcengine/doubao-embedding-large-text-240915`) returns 2048 dims,
  matching `VectorType = Vector(2048)` / `HalfvecType = HALFVEC(2048)`
  in `models.py`.

## Pipeline — `carrel/pipeline/embed.py`

`embed_paper(session, cfg, paper_id, on_progress=None)`:

1. Loads the Paper; requires `md_path` to exist on disk
   (`<storage.root>/<paper.md_path>`).
2. Idempotency: if the paper is already `ready` and has chunks, returns
   immediately. Otherwise resets `status='parsed'`, clears `error`, and
   **deletes all existing chunks for the paper in a single
   transaction** before re-embedding (so a re-embed never leaves stale
   rows).
3. Runs `chunk_markdown` with `cfg.chunking` parameters.
4. Calls `embeddings.embed_texts([c.content_md for c in chunks],
   model=cfg.embeddings.model, batch_size=cfg.embeddings.batch_size,
   timeout=...)`.
5. Writes one `Chunk` row per chunk (`paper_id`, `chunk_index`,
   `heading`, `content_md`, `token_count`, `embedding`).
6. On success sets `status='ready'`. A paper whose Markdown has no
   chunkable content is left at `parsed` with
   `error="no chunkable content in parsed markdown"`. Any other
   `EmbedError` sets `status='failed'` with the message on `paper.error`.

`select_pending_embed(session, limit)` returns in-library papers that
have an `md_path` and whose status is `parsed`, `summarized`, **or
`failed`** — a previously failed embed is retried because
`embed_paper` resets status to `parsed`, deletes stale chunks, and
starts over. The API endpoint `POST /embed` (`carrel/api/embed.py`)
wraps each paper in one `Job(kind='embed')`, mirroring `/process`.

## Vector storage and indexing

- `chunks.embedding` uses `Vector(2048).with_variant(JSON(), "sqlite")`
  so the ORM works in both Postgres and the in-memory SQLite used by
  tests. On Postgres the pgvector `vector` type is real; on SQLite the
  list is stored as JSON and cosine scoring runs in Python.
- `wiki_pages.embedding` uses `HALFVEC(2048)` (fp16) because
  pgvector's HNSW caps `vector` at 2000 dims and 2048 exceeds that;
  `halfvec` supports up to 4000 dims. The existing chunks table stays
  on `Vector(2048)` (sequential scan) to avoid a data migration.
- `init_db` creates two HNSW indexes best-effort at startup:
  - `ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding
    vector_cosine_ops)` — on pgvector versions that reject
    >2000-dim `vector`, the `OperationalError` is caught and logged;
    semantic search falls back to a sequential scan.
  - `ix_wiki_pages_embedding_hnsw ON wiki_pages USING hnsw (embedding
    halfvec_cosine_ops)` (requires pgvector ≥ 0.7.0).
- Both are `IF NOT EXISTS` so startup stays idempotent; HNSW defaults
  (`m=16`, `ef_construction=64`) are fine for <100k chunks (per the
  inline comment).

## Retrieval shape

Semantic search and per-paper chat both:

1. Embed the query with the same model (`embed_texts([q], batch_size=1)`).
2. On Postgres, order by
   `Chunk.embedding.cosine_distance(q_vec) <->` and convert distance to
   similarity (`1 - distance`).
3. On SQLite, decode the JSON-stored list and compute cosine in Python
   (see `_cosine` / `_decode_embedding` in `carrel/api/search.py`,
   reused by `carrel/api/chat.py`).

## Focused tests

- `tests/test_chunking.py` — heading stack, overlap windows,
  min-token merge, CJK word counting, preamble handling.
- `tests/test_embeddings.py` — env-key resolution, batching, empty
  input, zero-vector skip, retry behavior (litellm mocked).
- `tests/test_embed_pipeline.py` — idempotency, stale-chunk deletion,
  no-content handling, status transitions, on-disk markdown lookup.

## Validation

```bash
.venv/bin/python -m pytest tests/test_chunking.py tests/test_embeddings.py tests/test_embed_pipeline.py -q
```

## Evidence

- Chunker: `carrel/chunking.py`.
- Embeddings: `carrel/embeddings.py`.
- Pipeline: `carrel/pipeline/embed.py`.
- API: `carrel/api/embed.py`.
- Vector types and HNSW: `carrel/models.py`, `carrel/db.py`; see
  [../backend/database.md](../backend/database.md).
- Consumers: [../backend/search-and-chat.md](../backend/search-and-chat.md),
  [../wiki/overview.md](../wiki/overview.md) (wiki page embeddings).
