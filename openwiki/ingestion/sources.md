---
type: source_clients
title: Metadata source clients
description: The arXiv Atom, OpenAlex (pyalex), and Semantic Scholar httpx clients, plus the PaperRecord normalizer and the cross-source merge/RRF layer used by search.
tags: [sources, arxiv, openalex, semantic-scholar, normalization, merge, rrf]
---

# Metadata source clients

Carrel ingests metadata from three upstream services. Each client is
synchronous (the sync pipeline is serial; search fans them out in a
`ThreadPoolExecutor`). Normalization funnels source-specific dicts into
one `PaperRecord`, and a separate merge layer deduplicates search hits.

## arXiv — `carrel/sources/arxiv.py`

- Endpoint: `https://export.arxiv.org/api/query` (Atom 1.0).
- Adapted from `galleonli/paper-agent` (MIT).
- `ArxivEntry` dataclass is the raw parsed shape (id, title, summary,
  authors, categories, updated, abs_url, pdf_url).
- `fetch_recent(lookback_hours, categories=..., queries=..., max_results,
  timeout, delay_between_requests)` composes
  `(query) AND (cat:cs.CL OR ...)` searches, pages through results, and
  parses Atom XML with `xml.etree.ElementTree`.
- Rate-limit courtesy: exponential backoff starting at 20s on 429/503
  (max 3 retries), configurable `delay_between_requests` (default 3s,
  arXiv asks for ≥3s between calls).
- The arXiv id is always version-stripped downstream (`v1`, `v2`
  suffixes removed) for stable identity.

## OpenAlex — `carrel/sources/openalex_client.py`

Thin wrapper around the `pyalex` PyPI package. Carrel uses OpenAlex for:

1. Canonical Work/Author/Source IDs (the disambiguation spine).
2. `best_oa_location` PDF selection.
3. Recent works by author, venue, and keyword.
4. Work lookup by DOI / arXiv id (used by sync cross-id dedup and
   `/import`).
5. Venue name → Source-id resolution for the subscription editor.

`configure(cfg)` is idempotent and sets pyalex's `config.email` (polite
pool), optional `config.api_key`, connect/read timeouts, max retries,
and wraps pyalex's requests session with `_CappedRetry` — a urllib3
`Retry` subclass that clamps OpenAlex's potentially minutes-long
`Retry-After` to `_MAX_RETRY_AFTER_SECONDS = 5.0` so a budget-exhausted
429 fails fast instead of blocking the serial worker.

Field extraction helpers (`work_id`, `work_doi`, `work_arxiv_id`,
`work_venue`, `work_abstract`, `work_pdf_url`, `work_publication_date`,
`work_pdf_candidates`, `is_zenodo`) isolate pyalex's shape from the rest
of the codebase. `work_pdf_url` prefers `best_oa_location` then falls
through `locations`; `work_pdf_candidates` returns every plausible PDF
URL so the downloader can try them in order. Zenodo deposits are
filtered at the normalize layer (see below).

Author/venue/keyword search functions return raw pyalex Work dicts;
`fetch_author_works(aid, cursor, limit)` returns the works page plus the
opaque OpenAlex `next_cursor` and total count used by
`GET /scholars/{key}/works` (see
[../backend/scholars.md](../backend/scholars.md)).

## Semantic Scholar — `carrel/sources/semanticscholar_client.py`

Hand-rolled httpx client (no third-party package). Carrel uses S2 for:

- `citationCount`, `influentialCitationCount`, `referenceCount`.
- The capped list of citing papers and references for a paper
  (`/paper/{id}/citations`, `/paper/{id}/references`).
- Graph `/paper/search` (relevance, max 100/page) and
  `/paper/search/bulk` (token-paginated, up to 1000/page, supports
  citation/date sort) — both used by the external search fan-out.

Identity forms: `DOI:<doi>`, `ARXIV:<bare-id>`, or the 40-char S2
`paperId`. A module-level `_RateLimiter` token bucket enforces
`DEFAULT_RPS_WITH_KEY = 1.0` (with `x-api-key`) or
`DEFAULT_RPS_WITHOUT_KEY = 0.5` (the unauthenticated shared pool). S2's
`Retry-After` is capped at 30s.

The shared `httpx.Client` is configured once at startup from
`cfg.semantic_scholar` (see
[../backend/app-lifecycle.md](../backend/app-lifecycle.md)) and reused by
sync, citations, and search.

## Normalization — `carrel/sources/normalize.py`

`PaperRecord` is the in-process shape both sources reduce to:

```python
@dataclass(slots=True)
class PaperRecord:
    id: str            # OpenAlex W-id or "arxiv:<bare-id>"
    id_kind: str       # "openalex" | "arxiv"
    title: str
    abstract: str | None
    publication_date: date | None
    venue: str | None
    authors: list[dict]   # [{name, openalex_author_id, affiliation}]
    doi: str | None
    arxiv_id: str | None
    pdf_url: str | None
    oa_status: str        # "oa" | "closed" | "none"
    source: str           # "arxiv" | "openalex" | "both"
    raw_meta: dict
```

- `from_arxiv(entry)` — arXiv ids become `arxiv:<bare>`; authors have no
  A-ID yet (the authors-backfill pipeline resolves them later, see
  [../enrichment/authors-backfill.md](../enrichment/authors-backfill.md));
  OA status is `oa` when a PDF URL is present.
- `enrich_with_openalex(rec)` — best-effort OpenAlex lookup by arXiv id
  that attaches a W-id and upgraded metadata; on failure the record
  stays `arxiv:<id>`.
- `from_openalex(work)` — returns `None` for records that should be
  skipped, including Zenodo deposits (software/dataset DOIs on the
  Zenodo venue, which previously created duplicate concept/version rows).
- `is_zenodo(doi, venue)` is the single Zenodo gate used by both
  sync normalization and search.

## Cross-source search merge — `carrel/sources/merge.py`

This module is pure (no DB, no HTTP) and unit-tested directly. It
defines `SOURCE_OPENALEX`, `SOURCE_SEMANTIC_SCHOLAR`, `SOURCE_ARXIV`,
`SOURCE_LIBRARY`, and `MutableSearchHit` — a flat dataclass that holds
every field any source contributes plus a `sources: set[str]`, a
`ranks: dict[str,int]`, and library membership stamped by the API after
merge.

Dedup keys, in order: DOI → arXiv id → S2 paperId → OpenAlex W-id →
normalized title (last resort).

Field authority when two hits collide:

| Field | Winner |
|---|---|
| `citation_count` | Max across sources (S2 is freshest but not blindly trusted). |
| `venue` / `venue_type` | S2 first, then OA, then arXiv. |
| `authors` | First non-empty, preferring OpenAlex (A-IDs + affiliation). |
| `abstract` | First non-empty. |
| `pdf_url` | arXiv PDF (canonical, never a landing page), then OA, then S2. |
| `tldr` | S2 only. |
| Identifiers | Union — never drop an id a source contributed. |

`reciprocal_rank_fusion(hits, k=60)` sums `1/(k + rank)` across the
per-source ranks and sorts by that score. Local/library hits get a
synthetic rank of 1 from the search endpoint so they rank first under
RRF. Adapters (`from_openalex_work`, `from_s2_paper`, `from_arxiv_entry`,
`from_paper_row`) convert each raw source shape into `MutableSearchHit`.

## Focused tests

- `tests/test_arxiv_search.py` — query composition, Atom parsing,
  version stripping.
- `tests/test_openalex_client.py` — field extractors, Zenodo filter, PDF
  candidate ordering.
- `tests/test_s2_client.py` — rate limiter, id forms, retry/backoff,
  search/bulk response shapes.
- `tests/test_normalize.py` — `PaperRecord` reductions and Zenodo skip.
- `tests/test_search_merge.py` — pure merge field-authority and RRF
  ordering, including id-union semantics.

## Validation

```bash
.venv/bin/python -m pytest tests/test_arxiv_search.py tests/test_openalex_client.py tests/test_s2_client.py tests/test_normalize.py tests/test_search_merge.py -q
```

These tests use mocked HTTP; live source checks are manual via the
`/search/external` endpoint.

## Evidence

- `carrel/sources/arxiv.py`, `openalex_client.py`,
  `semanticscholar_client.py`, `normalize.py`, `merge.py`.
- Config: `carrel/config.py` (`ArxivConfig`, `OpenAlexConfig`,
  `SemanticScholarConfig`); see
  [../architecture/configuration.md](../architecture/configuration.md).
- Consumers: [sync.md](sync.md),
  [../backend/search-and-chat.md](../backend/search-and-chat.md),
  [../enrichment/citations.md](../enrichment/citations.md),
  [pdf-processing.md](pdf-processing.md).
