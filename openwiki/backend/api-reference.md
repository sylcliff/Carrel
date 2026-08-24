---
type: api_reference
title: HTTP API reference
description: Complete router table for every FastAPI endpoint, shared request/response patterns, background-job and progress-callback conventions, and links to the detailed domain pages.
tags: [api, rest, fastapi, reference, routing]
---

# HTTP API reference

All routes are mounted by `carrel/main.py:create_app` (see
[app-lifecycle.md](app-lifecycle.md)). There is no `/api` prefix on the
backend; Vite strips it from frontend calls (`frontend/vite.config.ts`).
JSON is the only wire format; FastAPI's Pydantic response models live in
`carrel/schemas.py`.

## Common patterns

- **Session dependency**: every route that touches the DB takes
  `session: Session = Depends(get_session_dep)`.
- **Background jobs**: trigger endpoints create one or more `Job` rows,
  then either run inline (`background=false`) or schedule a FastAPI
  `BackgroundTasks` worker that opens its own session. Progress is written
  to `job.stats["stage"]` / `["detail"]` by a callback; the frontend polls
  `GET /sync/jobs/{id}`.
- **One-job-per-paper**: `/process`, `/embed`, `/summarize`, `/topics`,
  `/papers/extract`, `/authors-backfill` create one Job per target paper
  when called with `paper_id`, or one per selected pending row when called
  with `limit`.
- **Whole-document PUTs**: notes and chat history replace the entire
  resource (no PATCH/diff). An empty/whitespace string clears the field.
- **Aliased reads**: any path that loads a paper by id first resolves it
  through `paper_dedup_ops.resolve_paper_id`, so a merged alias id
  transparently returns the canonical paper.

## Router table

| Method | Path | Purpose | Detail page |
|---|---|---|---|
| GET | `/health` | Version, DB ping, MinerU reachability, remote-SSH configured flag. | — |
| GET | `/papers` | List/filter/sort papers (8 sort modes; q/venue/favorite/in_library/status/tag/topic filters; batched tag/topic loader). | [papers-and-library.md](papers-and-library.md) |
| GET | `/papers/{paper_id}` | PaperDetail (resolves aliases). | [papers-and-library.md](papers-and-library.md) |
| POST | `/papers/{paper_id}/import` | Flip an inbox row `in_library=True`. | [papers-and-library.md](papers-and-library.md) |
| POST | `/papers/{paper_id}/discard` | Hide an inbox row (`discarded=True`; reversible by import). | [papers-and-library.md](papers-and-library.md) |
| DELETE | `/papers/{paper_id}` | Hard-delete paper, chunks, tag/topic links, chat, wiki sources, on-disk files (with `papers_root` safety guard). | [papers-and-library.md](papers-and-library.md) |
| GET | `/papers/{paper_id}/markdown` | Serve parsed Markdown body from disk. | [papers-and-library.md](papers-and-library.md) |
| POST | `/papers/{paper_id}/favorite` | Toggle `favorite`. | [papers-and-library.md](papers-and-library.md) |
| PUT | `/papers/{paper_id}/notes` | Replace `notes_markdown` (empty clears). | [papers-and-library.md](papers-and-library.md) |
| GET | `/papers/{paper_id}/tags` | List tags for a paper. | [papers-and-library.md](papers-and-library.md) |
| POST | `/papers/{paper_id}/tags` | Add a tag (case-insensitive dedup, first-seen casing wins). | [papers-and-library.md](papers-and-library.md) |
| DELETE | `/papers/{paper_id}/tags/{tag_id}` | Detach a tag from a paper. | [papers-and-library.md](papers-and-library.md) |
| GET | `/tags` | All tags with paper counts. | [papers-and-library.md](papers-and-library.md) |
| DELETE | `/tags/{tag_id}` | Delete a tag and detach it from all papers. | [papers-and-library.md](papers-and-library.md) |
| GET | `/papers/{paper_id}/citations` | Stored citing-paper list with library membership resolved. | [../enrichment/citations.md](../enrichment/citations.md) |
| GET | `/papers/{paper_id}/references` | Stored references (bibliography) with library membership. | [../enrichment/citations.md](../enrichment/citations.md) |
| POST | `/papers/{paper_id}/refresh-citations` | One Job (`citations`) running `enrich_paper`. | [../enrichment/citations.md](../enrichment/citations.md) |
| POST | `/papers/{paper_id}/chat` | SSE stream of RAG answer tokens; first frame lists source headings. | [search-and-chat.md](search-and-chat.md) |
| GET | `/papers/{paper_id}/chat/messages` | Persisted transcript. | [search-and-chat.md](search-and-chat.md) |
| PUT | `/papers/{paper_id}/chat/messages` | Replace the transcript (whole-document PUT). | [search-and-chat.md](search-and-chat.md) |
| POST | `/papers/{paper_id}/check-publication` | One Job checking arXiv → journal DOI. | [../ingestion/publication-check.md](../ingestion/publication-check.md) |
| POST | `/papers/extract` | One Job per paper running `paper_extract` (concepts + questions). | [../wiki/paper-extract.md](../wiki/paper-extract.md) |
| GET | `/subscriptions` | List subscriptions. | — |
| POST | `/subscriptions` | Create subscription (kind/value/label). | — |
| DELETE | `/subscriptions/{sub_id}` | Delete subscription. | — |
| POST | `/subscriptions/top-journals` | One-click add Nature/Cell/Science. | — |
| GET | `/schedule` | Scheduler status, next-run, last result per `JOB_SPECS`. | [../architecture/scheduler-and-jobs.md](../architecture/scheduler-and-jobs.md) |
| PATCH | `/schedule` | Flip enable / edit cron; writes YAML atomically and restarts scheduler. | [../architecture/configuration.md](../architecture/configuration.md) |
| POST | `/schedule/{job_id}/run` | Manual run (in-flight guard). | [../architecture/scheduler-and-jobs.md](../architecture/scheduler-and-jobs.md) |
| POST | `/sync` | Queue/run a sync pass (one Job). | [../ingestion/sync.md](../ingestion/sync.md) |
| GET | `/sync/jobs` | List jobs (filter by kind/status). | [../architecture/scheduler-and-jobs.md](../architecture/scheduler-and-jobs.md) |
| GET | `/sync/jobs/{job_id}` | One job's current state/stats. | [../architecture/scheduler-and-jobs.md](../architecture/scheduler-and-jobs.md) |
| POST | `/process` | Download+parse one paper or a batch of pending/failed papers (one Job/paper). | [../ingestion/pdf-processing.md](../ingestion/pdf-processing.md) |
| POST | `/summarize` | LLM bilingual TL;DR/summary/keywords (one Job/paper). | [../enrichment/summarization.md](../enrichment/summarization.md) |
| POST | `/topics` | LLM topic classification (one Job/paper). | [../enrichment/topics.md](../enrichment/topics.md) |
| GET | `/topics` | All topics with paper counts. | [../enrichment/topics.md](../enrichment/topics.md) |
| POST | `/embed` | Chunk+embed parsed papers (one Job/paper). | [../enrichment/embeddings.md](../enrichment/embeddings.md) |
| POST | `/authors-backfill` | Resolve missing OpenAlex A-IDs (one Job/paper). | [../enrichment/authors-backfill.md](../enrichment/authors-backfill.md) |
| GET | `/search/local` | SQL ILIKE over library papers. | [search-and-chat.md](search-and-chat.md) |
| GET | `/search/external` | OA+S2+arXiv fan-out, merged/deduped, with filters. | [search-and-chat.md](search-and-chat.md) |
| GET | `/search` | Combined local + external with RRF/citation/date sort. | [search-and-chat.md](search-and-chat.md) |
| GET | `/search/semantic` | pgvector semantic search over embedded chunks. | [search-and-chat.md](search-and-chat.md) |
| POST | `/import` | Import a paper from OA/S2 by any id (OA W-id, DOI, arXiv, S2). | [search-and-chat.md](search-and-chat.md) |
| GET | `/scholars` | Aggregated scholars (cached, library-signature invalidation). | [scholars.md](scholars.md) |
| GET | `/scholars/{key}` | Scholar detail + OpenAlex profile + compiled wiki page. | [scholars.md](scholars.md) |
| GET | `/scholars/{key}/works` | Paged OpenAlex works with library membership (opaque cursor; 422 for name-only scholars). | [scholars.md](scholars.md) |
| GET | `/wiki/pages` | List wiki pages (filter by kind, stub, etc.). | [../wiki/overview.md](../wiki/overview.md) |
| GET | `/wiki/pages/{page_id}` | Page detail (frontmatter + body + sources + backlinks). | [../wiki/overview.md](../wiki/overview.md) |
| GET | `/wiki/pages/by-kind-slug/{kind}/{slug}` | Lookup by catalog address. | [../wiki/overview.md](../wiki/overview.md) |
| POST | `/wiki/compile` | One Job running the four-stage batch (extract → scholar → concept → question). | [../wiki/compilers.md](../wiki/compilers.md) |
| POST | `/wiki/pages/{page_id}/recompile` | Force-recompile one page. | [../wiki/compilers.md](../wiki/compilers.md) |
| GET | `/paper-dedup/suggestions` | Current duplicate-paper suggestions (cached scoring pass). | [../dedup/paper-dedup.md](../dedup/paper-dedup.md) |
| POST | `/paper-dedup/run` | One Job running deterministic + LLM judge scoring, auto-applying high-confidence merges. | [../dedup/paper-dedup.md](../dedup/paper-dedup.md) |
| POST | `/paper-dedup/merge` | Manually accept a suggestion. | [../dedup/paper-dedup.md](../dedup/paper-dedup.md) |
| POST | `/paper-dedup/reject` | Record a reject (suppresses future auto-suggestion). | [../dedup/paper-dedup.md](../dedup/paper-dedup.md) |
| DELETE | `/paper-dedup/aliases/{alias_paper_id}/{canonical_paper_id}` | Undo a merge. | [../dedup/paper-dedup.md](../dedup/paper-dedup.md) |
| POST | `/paper-dedup/judge` | Run the LLM judge for one pair on demand. | [../dedup/paper-dedup.md](../dedup/paper-dedup.md) |
| GET | `/scholar-dedup/suggestions` | Same-name A-ID duplicate suggestions. | [../dedup/scholar-dedup.md](../dedup/scholar-dedup.md) |
| POST | `/scholar-dedup/run` | One Job running same-name scoring + auto-merge. | [../dedup/scholar-dedup.md](../dedup/scholar-dedup.md) |
| POST | `/scholar-dedup/merge` | Accept a scholar-alias suggestion. | [../dedup/scholar-dedup.md](../dedup/scholar-dedup.md) |
| POST | `/scholar-dedup/reject` | Reject a scholar-alias suggestion. | [../dedup/scholar-dedup.md](../dedup/scholar-dedup.md) |
| DELETE | `/scholar-dedup/aliases/{alias_aid}/{canonical_aid}` | Undo a scholar merge. | [../dedup/scholar-dedup.md](../dedup/scholar-dedup.md) |
| GET | `/storage/...` | StaticFiles mount over `cfg.storage.root` (papers, wiki images). | [../architecture/configuration.md](../architecture/configuration.md) |

## Pydantic request/response highlights

- **Batch job requests** share the shape
  `{paper_id?: str, limit?: int, background?: bool = true, force?: bool}`.
- **`JobOut`** is returned by every trigger endpoint: `id`, `kind`, `status`,
  `message`, `stats`, `started_at`, `finished_at`, `created_at`.
- **`PaperSummary`** is the list-card projection; **`PaperDetail`** adds
  abstract, identifiers, paths, author_list, citations timestamps, notes,
  pdf_files, journal_doi, and created/updated timestamps.
- **`SearchResponse`** wraps `results: list[SearchResultItem]`, `query`
  (post-spell-correction), `corrected_from`, and `warnings` (per-source
  errors; a failed source never 500s the request).

## Validation

```bash
# OpenAPI JSON is served by FastAPI at /openapi.json when the app runs.
curl -s http://127.0.0.1:8787/openapi.json | jq '.paths | keys'
.venv/bin/python -m pytest tests/test_api.py tests/test_process_api.py tests/test_summarize_api.py tests/test_topics_api.py tests/test_citations_api.py tests/test_annotations_api.py tests/test_inbox_api.py tests/test_scholar_works.py tests/test_wiki_api.py tests/test_search_api.py tests/test_chat_history_api.py -q
```

## Evidence

- Router registrations and order: `carrel/main.py:152-171`.
- All routers: `carrel/api/*.py`.
- Schemas: `carrel/schemas.py`.
- Background-job convention representative: `carrel/api/process.py`,
  `carrel/api/sync.py`, `carrel/api/embed.py`.
- Frontend client (single source of truth for paths from the browser):
  `frontend/src/api/client.ts`.
