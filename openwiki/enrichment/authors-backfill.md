---
type: pipeline
title: Author A-ID backfill
description: Resolves missing OpenAlex Author IDs on in-library papers by exact DOI/arXiv lookup, repairing fragmented Scholar pages without fuzzy name matching.
tags: [authors, openalex, disambiguation, backfill]
---

# Author A-ID backfill

`carrel/pipeline/authors.py` resolves author OpenAlex A-IDs for papers
that were imported from Semantic Scholar (which often stores only
abbreviated names like "G. Chan" without an A-ID). Without A-IDs, the
Scholars browse page ([../backend/scholars.md](../backend/scholars.md))
can only group by exact name string, so the same person ("G. Chan" vs
"Garnet Kin-Lic Chan") is split across several entries. The deeper
cross-A-ID dedup lives in
[../dedup/scholar-dedup.md](../dedup/scholar-dedup.md); this pipeline
populates the A-IDs that dedup builds on.

## Selection

`select_pending(session, limit=100)` returns in-library, non-discarded
papers where at least one author dict lacks an `openalex_author_id` and
the paper has a DOI or arXiv id to look up.

## Backfill

`backfill_paper(session, cfg, paper, on_progress=None)`:

1. Looks up the canonical OpenAlex Work by DOI (preferred) or arXiv id.
2. Reads the Work's authorship list in OpenAlex order and writes back
   `{name, openalex_author_id, affiliation}` for every author —
   replacing the abbreviated names with the canonical display names and
   institutions.
3. Is **authoritative, not fuzzy**: the only match signal is the exact
   DOI/arXiv id, so two different people sharing initials can never be
   merged incorrectly here. (Cross-A-ID same-person merging is a
   separate, scored step in scholar-dedup.)
4. Is idempotent: papers whose authors all have A-IDs are skipped.
5. Is polite: `_REQUEST_SLEEP = 0.4` between OpenAlex requests.
6. Is non-fatal: a lookup failure leaves `paper.authors` unchanged and
   is logged; it does not flip `paper.status` or `paper.error`.

## Entry points

- `POST /authors-backfill` in `carrel/api/authors_backfill.py` wraps
  each selected paper in one `Job(kind='authors_backfill')`, supports
  a specific `paper_id`, a `limit`, and inline/background execution
  like the other per-paper endpoints.
- There is no scheduled entry for authors-backfill at the time of
  writing; it runs on demand from the Scholars page. (The scheduler's
  `JOB_SPECS` covers sync, remote_fill, publication_check, and
  wiki_compile only.)

## Related systems

- After A-IDs are populated, scholar-dedup can score same-name A-ID
  clusters and write `ScholarAlias` rows
  ([../dedup/scholar-dedup.md](../dedup/scholar-dedup.md)).
- The Scholars aggregation itself lives in
  `carrel/pipeline/wiki/_scholars_agg.py` and is documented on
  [../backend/scholars.md](../backend/scholars.md).
- Authors are stored as a JSON column on `Paper` (no Author table);
  see [../architecture/data-model.md](../architecture/data-model.md).

## Focused tests

- `tests/test_scholar_works.py` and `tests/test_scholar_compile.py`
  exercise the downstream effects of resolved A-IDs. The
  `tests/test_scholar_dedup.py` suite covers same-name clustering.
- The backfill module is currently tested indirectly through those
  suites; there is no dedicated `test_authors_backfill.py`.

## Validation

```bash
.venv/bin/python -m pytest tests/test_scholar_works.py tests/test_scholar_dedup.py -q
```

## Evidence

- Pipeline: `carrel/pipeline/authors.py`.
- API: `carrel/api/authors_backfill.py`.
- OpenAlex lookup: `carrel/sources/openalex_client.py`
  (`lookup_by_doi`, `lookup_by_arxiv_id`).
- Downstream: [../dedup/scholar-dedup.md](../dedup/scholar-dedup.md),
  [../backend/scholars.md](../backend/scholars.md).
