---
type: domain_page
title: Scholars aggregation and API
description: How authors are aggregated across in-library papers, the TTL-cached /scholars endpoints, OpenAlex profile lookup, the /works cursor pagination, and the compiled wiki-page join.
tags: [scholars, authors, aggregation, openalex, caching, wiki]
---

# Scholars aggregation and API

Authors are stored only as a JSON list on each `Paper` row
(`[{name, openalex_author_id, affiliation}]`). There is no `Author` table.
The Scholars browse surface aggregates that column at request time
(personal-library scale), groups by OpenAlex Author ID when available and
by normalized name otherwise, and joins in a live OpenAlex profile and an
LLM-compiled wiki page.

## Aggregation keys

`carrel/pipeline/wiki/_scholars_agg.py:author_key` is the canonical key
function, shared by the API and the wiki compiler:

- If an author has a non-empty `openalex_author_id`, the A-ID is resolved
  through the `scholar_aliases` table
  (`carrel/pipeline/scholar_dedup.resolve_aid`) so duplicate OpenAlex
  profiles collapse. The result is the canonical A-ID string
  (e.g. `A5013214678`).
- Otherwise the key is `name:<normalized-name>`, where
  `pipeline.wiki._names.normalize_name` strips punctuation and collapses
  whitespace (so `"He Li"`, `"He-Li"`, and `"he  li"` share one key). The
  display name is chosen from the most common raw spelling in the
  aggregation's Counter.
- A short-lived `_alias_cache` memoizes A-ID resolution within one
  aggregation batch; it is cleared by `invalidate_alias_cache` after every
  merge/reject.

`aggregate(session)` returns one `ScholarSummary` per key with paper count,
first/last year, total citations, and an `has_openalex` flag.
`papers_for_key(session, key)` returns the in-library Paper rows for a key
(used by the detail page and the wiki compiler).

## List endpoint and TTL cache

`GET /scholars` (`carrel/api/scholars.py`) supports `q` (case-insensitive
substring on name) and `limit` (100 default, capped at 500). The aggregation
itself is wrapped in an in-process cache:

- `_list_cache = {"ts", "sig", "items"}` guarded by `_list_lock`.
- `_LIST_TTL = 60.0` seconds.
- `_library_signature(session)` returns `(max(papers.updated_at),
  count(in-library, not discarded))`. If the signature changes (a paper was
  added/edited/imported/deleted) the cache is rebuilt immediately even if
  the TTL has not elapsed.
- Both branches sort by local paper count desc.

This keeps the Scholars page cheap (the library is small) while guaranteeing
that an import or note edit shows up within one request.

## OpenAlex profile cache

`get_profile(key)` (in `_scholars_agg`) fetches an OpenAlex Author record
(works_count, cited_by_count, h_index, affiliation, etc.) for A-ID keys.
The result is cached in `_profile_cache` for `_PROFILE_TTL = 24h`. The
cache is shared by the `/scholars/{key}` endpoint and the wiki scholar
compiler, so a compile and a page view never refetch the same profile.
Name-only keys return no profile.

## Detail endpoint

`GET /scholars/{key}` returns `ScholarDetail`:

- `scholar`: the `ScholarSummary` (looked up in the cached aggregation; a
  key not present in the local library returns 404, so the endpoint cannot
  be used to enumerate arbitrary OpenAlex authors).
- `papers`: the in-library papers for that key as `PaperSummary` rows.
- `profile`: the cached OpenAlex profile or `null`.
- `wiki_page`: the compiled scholar wiki page joined via `_scholar_wiki_page`
  (see below), or `null`.

### Wiki page join

`_scholar_wiki_page(session, key, name)` looks up a live (non-redirect)
`WikiPage`:

1. Builds the `entity_key` (`scholar:<aid>` or `scholar:name:<name>`) and
   queries by that — the partial unique index makes this the fast path and
   survives A-ID / name-only transitions.
2. If nothing is found it falls back to `(kind='scholar', slug=<scholar_slug>)`
   for legacy rows whose `entity_key` was never populated; those are
   reconciled on the next startup pass (see
   [../wiki/reconciliation.md](../wiki/reconciliation.md)).

The `WikiPage` row is then expanded through `carrel.api.wiki._page_detail`,
which reads the Markdown body from disk, parses frontmatter, attaches
`WikiSource` provenance and backlinks.

## Works pagination

`GET /scholars/{key}/works` pages the scholar's complete OpenAlex works
list (newest first), joined with the local library:

- **422 for name-only scholars.** Only A-ID keys can be resolved through
  OpenAlex; the response tells the user to run "Resolve authors" first
  (see [../enrichment/authors-backfill.md](../enrichment/authors-backfill.md)).
- **Opaque cursor.** `cursor` is OpenAlex's `meta.next_cursor`; the route
  does not parse or construct it. `limit` is capped at 50.
- **Total count** is OpenAlex's reported count for the author (same on
  every page) so the UI can render "Showing X of Y".
- **Three-key library match.** `_batch_library_match` issues one query
  over the collected OpenAlex W-ids, lowercased DOIs, and arXiv ids and
  indexes the rows by all three identifiers, avoiding N+1 lookups per work.
  Each item is annotated with `in_library` (library or inbox) and
  `library_id`.

## Focused tests

- `tests/test_scholar_works.py` — cursor pagination, name-only 422,
  library matching by DOI / arXiv / OpenAlex id.
- `tests/test_scholar_compile.py` — uses the same `aggregate` /
  `papers_for_key` primitives and exercises A-ID resolution through the
  alias table.
- `tests/test_scholar_dedup.py` exercises the alias resolution that
  `author_key` depends on.

## Validation

```bash
.venv/bin/python -m pytest tests/test_scholar_works.py tests/test_scholar_compile.py tests/test_scholar_dedup.py -q
```

## Evidence

- API routes and caches: `carrel/api/scholars.py`.
- Pure aggregation and profile cache:
  `carrel/pipeline/wiki/_scholars_agg.py`.
- Name normalization and slug: `carrel/pipeline/wiki/_names.py`,
  `carrel/pipeline/wiki/_slug.py`.
- OpenAlex author works fetch:
  `carrel/sources/openalex_client.py:fetch_author_works`.
- Frontend: `frontend/src/pages/Scholars.tsx`,
  `frontend/src/pages/ScholarDetail.tsx`.
- Related: [../wiki/compilers.md](../wiki/compilers.md),
  [../dedup/scholar-dedup.md](../dedup/scholar-dedup.md),
  [../enrichment/authors-backfill.md](../enrichment/authors-backfill.md).
