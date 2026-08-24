---
type: pipeline
title: Citation enrichment
description: Semantic Scholar-driven citation and reference enrichment for in-library papers, with OpenAlex cites-union fallback, bounded sync sweeps over stale and reference-less papers, and library-membership resolution in the citations API.
tags: [citations, semantic-scholar, references, enrichment]
---

# Citation enrichment

`carrel/pipeline/citations.py` populates the S2-sourced citation fields
on `Paper`:

- `s2_paper_id`
- `citation_count`, `influential_citation_count`, `reference_count`
- `citing_papers` — capped list (default 500) of
  `{title, year, doi, arxiv_id, s2_paper_id, openalex_id, venue}`
- `references` — the paper's bibliography, same item shape
- `citations_updated_at` — throttle/staleness timestamp

S2 is the primary source; OpenAlex's `cites:` endpoint contributes any
extra citing works S2 missed, then both are deduped.

## Enrichment

`enrich_paper(session, cfg, paper_id, on_progress=None)`:

1. Resolves the S2 paper id from `paper.s2_paper_id`, DOI (`DOI:<doi>`),
   or arXiv id (`ARXIV:<bare>`).
2. `s2.fetch_paper(id)` for counts and `s2.fetch_citations(id, limit)` /
   `s2.fetch_references(id, limit)` for the lists. Request pacing is
   handled globally by the S2 client's token-bucket rate limiter (see
   [../ingestion/sources.md](../ingestion/sources.md)).
3. `oa.fetch_citing_works(doi_or_oa_id, limit=200)` returns any
   additional OpenAlex citing works, normalized via
   `_openalex_to_citing` and merged in `_merge_citing`. S2 entries win
   on conflict (richer venue/tldr), but when an S2 entry is missing a
   venue and the OA match has one, it is backfilled.
4. Dedup keys in order: DOI → arXiv id → S2 id → OpenAlex id →
   normalized title.
5. Writes the merged lists, counts, and `citations_updated_at=now`.

Failures are soft: a network or rate-limit error is logged and
re-raised so a single-paper Job can be marked failed, but batch callers
(sync) catch and continue so one bad lookup never aborts a run.

## Batch selection (sync)

Two bounded selectors drive the citation sweeps inside
[`run_sync`](../ingestion/sync.md):

- `select_missing_references(session, limit)` — library papers with
  `reference_count` set but `references IS NULL` (rows enriched before
  the references-list feature shipped).
- `select_stale(session, limit)` — the stalest library papers by
  `citations_updated_at NULLS FIRST`, so cited-by counts creep forward
  without re-hitting the whole library every night. Default
  `cfg.semantic_scholar.citations_refresh_batch = 25`; set to 0 to
  disable periodic refresh.

Both lists are de-duplicated while preserving order
(missing-references first) before `enrich_papers` is called.

## API — `carrel/api/citations.py`

- `GET /papers/{paper_id}/citations` returns `CitationListOut` — the
  stored citing-paper list with each item's `in_library` / `library_id`
  resolved. `_resolve_library` builds identifier → Paper.id lookups in
  one batched query across DOI (including `https://doi.org/` variants),
  arXiv id, S2 id, and OpenAlex id (including full URL variants) so
  long lists don't trigger N+1 queries.
- `GET /papers/{paper_id}/references` is the equivalent for the
  bibliography.
- `POST /papers/{paper_id}/refresh-citations` creates one
  `Job(kind='citations')` and runs `enrich_paper` inline or in a
  `BackgroundTasks` thread; the frontend polls the Job the same way it
  polls a parse job.

## Config

- `cfg.semantic_scholar.citations_limit` (default 500) — cap on the
  stored citing-paper list.
- `cfg.semantic_scholar.fetch_on_sync` (default true) — master switch
  for the sync sweeps.
- `cfg.semantic_scholar.references_backfill_batch` (default 50).
- `cfg.semantic_scholar.citations_refresh_batch` (default 25).

## Focused tests

- `tests/test_citations_api.py` — `GET /citations` and
  `GET /references` shaping, batched library-membership resolution
  across all four id forms, refresh-job creation.

The underlying pipeline is exercised through
`tests/test_s2_client.py` (S2 shaping) and `tests/test_runner.py`
(sync sweep selection and failure isolation).

## Validation

```bash
.venv/bin/python -m pytest tests/test_citations_api.py tests/test_s2_client.py -q
```

## Evidence

- Pipeline: `carrel/pipeline/citations.py`.
- S2/OpenAlex clients: `carrel/sources/semanticscholar_client.py`,
  `openalex_client.py` ([../ingestion/sources.md](../ingestion/sources.md)).
- API: `carrel/api/citations.py`.
- Data model: `Paper.citation_count`, `Paper.citing_papers`,
  `Paper.references`, `Paper.citations_updated_at` in
  [../architecture/data-model.md](../architecture/data-model.md).
- Frontend: `frontend/src/components/CitationsCard.tsx`,
  `ReferencesCard.tsx`, `CitationRowActions.tsx`.
