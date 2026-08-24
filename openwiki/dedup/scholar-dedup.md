---
type: pipeline
title: Scholar dedup
description: Second-pass author disambiguation that clusters same-named OpenAlex A-IDs, scores coauthor/affiliation/topic overlap, and writes ScholarAlias rows (auto/user/reject) so the scholar aggregator and wiki treat duplicates as one person.
tags: [dedup, scholars, aliases, openalex, disambiguation]
---

# Scholar dedup

`carrel/pipeline/scholar_dedup.py` handles the case where OpenAlex
splits one real researcher across several Author IDs — common for
Chinese names and early-career authors. Unlike the
[author backfill](../enrichment/authors-backfill.md) (which resolves
A-IDs from exact DOI/arXiv matches), this module looks *across*
A-IDs and asks whether two are the same person.

## Inputs

Only **in-library, non-discarded** papers are scanned
(`_iter_author_records`). For each author dict with an
`openalex_author_id`, the pipeline collects an `AidEvidence`:

- `paper_ids`, `years`, `names` (all surface spellings seen)
- `coauthor_aids`, `coauthor_names`
- `institutions` (normalized via `_norm_affil`: lowercase,
  `university → univ`, `(institute|institution) → inst`, stopwords
  stripped)

## Clustering

`build_clusters(session)` groups A-IDs by their normalized display
name (`_norm_name` strips whitespace/dots/CJK spacing differences).
Only clusters with **more than one A-ID** are interesting; singletons
are dropped.

## Pairwise scoring

For each pair of A-IDs in a same-name cluster, `score_pair` combines
signals:

- **Coauthor overlap** is the strongest signal — two profiles of the
  same person tend to share collaborators. Two measures are used:
  - Jaccard over coauthor A-IDs (`_jaccard`), used in the weighted
    total.
  - Overlap coefficient `intersection / min(|A|,|B|)`
    (`_overlap`), which is 1.0 when the smaller profile's
    collaborators are all subsumed by the larger one — important for
    1-paper A-IDs where Jaccard punishes the small denominator.
- **Affiliation** normalized equality.
- **Topic overlap** Jaccard over the author's top OpenAlex topic IDs
  (fetched via `oa.fetch_author`, cached in-process for 24h with
  polite `_REQUEST_SLEEP = 0.25` pacing).
- **Works-count ratio** — a 1-paper ID next to a 50-paper ID is a
  more plausible duplicate than two equally-prolific IDs.
- **Name overlap** — exact display name → 1.0; token-subset
  ("Y. Xu" vs "Yong Xu") → 0.6; otherwise 0.3 (they already share
  the normalized cluster key).

## Strong anchors and auto-merge

Strong-anchor constants (any one is sufficient):

- `STRONG_COAUTHOR_OVERLAP = 0.6` (overlap coefficient)
- `STRONG_COAUTHOR_JACCARD = 0.34` (for two well-populated profiles)
- `STRONG_AFFIL_AND_COAUTHOR = (1.0, 0.05)` — same affiliation plus
  any coauthor overlap.

The cap `MANY_PAPERS = 5` raises the bar when both A-IDs have that
many in-library papers: two well-published namesakes at the same
institution can be different people, so don't auto-merge on
institution alone.

Pairwise scores feed a union-find; components whose best pair score
exceeds `AUTO_CONFIDENCE = 0.55` are persisted as `ScholarAlias`
rows with `source='auto'`. Pairs the user has explicitly rejected
(`source='reject'`) are never rejoined.

`resolve_aid(session, aid)` follows the alias chain (with a hop cap)
to its canonical root; this is what
`carrel/pipeline/wiki/_scholars_agg.author_key` calls so that wiki
pages and the Scholars page treat aliases as one person.

## User-visible suggestions

Pairs below the auto-merge threshold are returned as *suggestions*
for the UI to expose with Accept/Reject. The API
(`carrel/api/scholar_dedup.py`) caches the last scoring pass
in-process (`_cached_suggestions`, lock-guarded); merge/reject
update the affected pair without a full rescore because scoring fans
out one OpenAlex Authors call per A-ID (~10s for ~70 A-IDs).

`POST /scholar-dedup/run` runs the scan as a `scholar_dedup` Job,
`GET /scholar-dedup/suggestions` reads the cache, and
`/merge`, `/reject`, `DELETE /aliases/...` write or remove
`ScholarAlias` rows with `source='user'`/`'reject'` and
`confidence=1.0`.

## Invariants

- `Paper.authors` is **never rewritten** — aliases are an
  indirection resolved by `author_key`, keeping original provenance
  intact.
- A merge is reversible: delete the alias row and the A-ID stands
  alone again.
- `reject` overrides an earlier `auto` merge; alias rows are never
  deleted on re-dedup.
- The unique index
  `ix_scholar_aliases_alias_canon (alias_aid, canonical_aid)`
  prevents duplicate rows.

## Focused tests

- `tests/test_scholar_dedup.py` — name clustering, coauthor Jaccard
  and overlap coefficient, strong-anchor gating, union-find,
  reject-suppression, alias resolution.
- `tests/test_scholar_works.py` and `tests/test_scholar_compile.py`
  exercise the downstream effect of aliases on aggregation and wiki
  pages.

## Validation

```bash
.venv/bin/python -m pytest tests/test_scholar_dedup.py -q
```

## Evidence

- Pipeline: `carrel/pipeline/scholar_dedup.py`.
- API: `carrel/api/scholar_dedup.py`.
- Table: `ScholarAlias` in `carrel/models.py`.
- Resolution site: `carrel/pipeline/wiki/_scholars_agg.author_key`,
  `resolve_aid`.
- OpenAlex profile fetcher: `carrel/sources/openalex_client.py`
  (`fetch_author`).
- Wiki reconciliation interaction:
  [../wiki/reconciliation.md](../wiki/reconciliation.md).
