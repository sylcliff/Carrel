---
type: pipeline
title: Paper dedup
description: Deterministic scoring, union-find clustering, optional LLM judge, and PaperAlias indirection for duplicate paper rows, with user-state migration and reversible merges.
tags: [dedup, papers, aliases, llm-judge, union-find]
---

# Paper dedup

`carrel/pipeline/paper_dedup.py` (and its helpers in
`paper_dedup_ops.py`, `paper_dedup_judge.py`) catches cases where the
same paper has multiple `papers` rows because of cross-id collisions
(DOI vs arXiv id vs S2 paperId vs journal-doi bridge, or the same work
ingested under different OpenAlex/arXiv primary keys). It mirrors the
[scholar-dedup](scholar-dedup.md) shape but with paper-specific
signals, an optional LLM judge for the borderline band, and heavier
user-state migration on merge.

## Strong anchors (auto-merge)

`_detect_strong_anchors(a, b)` returns a list of anchor names; **any
one is sufficient to treat the pair as the same paper**:

- `doi` — exact match after `_clean_doi` normalization.
- `arxiv` — exact match after version strip (`v1`/`v2`).
- `journal_doi_bridge` — A and B share an arXiv id and A's
  `journal_doi` equals B's `doi` (or vice versa): the arXiv preprint
  and its published journal version.
- `s2` — exact match of `s2_paper_id`.
- `llm` — the LLM judge returned `same` with confidence
  ≥ `LLM_SAME_THRESHOLD = 0.85`.

## Soft signals (weighted, when no strong anchor fires)

- title Jaccard on normalized tokens: `_W_TITLE = 0.45`.
- author overlap (A-ID preferred, name fallback):
  `_W_AUTHORS = 0.30`.
- same publication year: `_W_YEAR = 0.10`.
- same venue (normalized): `_W_VENUE = 0.10`.
- abstract prefix overlap (first 200 chars): `_W_ABSTRACT = 0.05`.

## Thresholds

- `AUTO_CONFIDENCE = 0.65` — pairs at or above this are auto-merged.
- `LLM_BORDERLINE_LO = 0.50`, `LLM_BORDERLINE_HI = 0.65` — pairs in
  this band trigger the LLM judge (when one is configured).
- `LLM_SAME_THRESHOLD = 0.85` — an LLM verdict of `same` at or above
  this confidence counts as a strong anchor.

Candidates are bucketed by a normalized title prefix
(`_TITLE_PREFIX = 20` chars) so the pairwise scan stays bounded; the
full normalized title is then Jaccard-scored.

## Union-find and canonical pick

Pairwise scores feed a union-find over paper ids. Components above
`AUTO_CONFIDENCE` are auto-merged; the canonical winner is picked by
an ordering that prefers OpenAlex-ID rows over arXiv placeholders and
richer metadata over sparser rows. The loser is recorded as a
`PaperAlias(alias_paper_id, canonical_paper_id, source, confidence,
reasons)`.

## LLM judge — `paper_dedup_judge.py`

Three implementations of the `PaperPairJudge` protocol:

- `DeterministicJudge` — uses only strong anchors (CI-friendly, no
  network).
- `LLMJudge` — calls the configured LLM with a structured
  same/different/uncertain prompt and caches verdicts symmetrically
  (stored as `(min(a,b), max(a,b))`) in the `paper_dedup_verdicts`
  table. The cache key includes a `prompt_hash` over input + model +
  `cfg.llm.paper_dedup_judge_prompt_version`, so bumping the version
  invalidates verdicts without touching the pair. A per-run budget
  `paper_dedup_judge_max_calls_per_run` (default 200) prevents a large
  borderline queue from running the meter away.
- `CompositeJudge` — strong anchors short-circuit to `same`;
  borderline pairs are routed to the LLM. Without a configured LLM it
  is pure deterministic.

## Alias indirection and user-state migration — `paper_dedup_ops.py`

`apply_merge` is the single import surface for "merge two papers into
one", used by the pipeline, the API, and the migration script:

1. `resolve_paper_id(session, paper_id)` follows the alias chain to
   its root (cap `_MAX_ALIAS_HOPS = 8`, cycle-safe). `source='reject'`
   aliases are **not** followed — a rejected pair stays separate.
   Every read path goes through this so the alias is transparent to
   API consumers.
2. The loser row is **kept** in `papers` (never physically deleted).
   Its user state is migrated to the canonical:
   - scalar columns in `_USER_STATE_FIELDS` (favorite, notes, tldrs,
     keywords, in_library, discarded, discovered_at).
   - `PaperTag` and `PaperTopic` association rows moved (collisions
     dropped).
   - `ChatMessage` rows reparented.
   - `Chunk` rows reparented (the winner keeps the richer embedding
     set if both have chunks).
   - `WikiSource` rows reparented.
   - citation/reference lists merged by id.
3. A `PaperMergeEvent` row snapshots the loser's pre-migration state
   (including counts of tags/topics/chunks/chat messages/wiki
   sources) for audit and a possible future "undo with state
   restore".
4. The loser's user-state columns are cleared and its `status` set
   to `PaperStatus.merged.value`. The row is read through
   `resolve_paper_id`, so it is effectively hidden.
5. A merge is reversible by deleting the alias row (the user_state
   that already moved to the canonical is not put back; see the
   `PaperMergeEvent` snapshot).

## Suggestions and API

Pairs below `AUTO_CONFIDENCE` (and not rejected) are returned as
*suggestions* for the UI. `carrel/api/paper_dedup.py` caches the last
scoring pass in-process (deterministic by default, no network),
invalidates on merge/reject/undo, and exposes:

- `GET /paper-dedup/suggestions` — cached snapshot.
- `POST /paper-dedup/run` — run as a `paper_dedup` Job
  (`auto_apply=True` applies high-confidence merges).
- `POST /paper-dedup/merge`, `/reject`,
  `DELETE /paper-dedup/aliases/{a}/{b}` — write `user`/`reject`
  aliases (confidence 1.0) or undo.
- `POST /paper-dedup/judge` — run the LLM judge on a single pair on
  demand.

The UI surface is the Library page "Duplicates" panel
(`frontend/src/components/PaperDedupPanel.tsx`).

## Entry points

- `POST /paper-dedup/run` (manual; there is no scheduled cron for
  paper dedup at the time of writing).
- `scripts/migrate_paper_dedup.py` — one-shot script that scans the
  library and auto-merges strong-anchor duplicates; covered by
  `tests/test_migrate_paper_dedup.py`.

## Focused tests

- `tests/test_paper_dedup.py` — strong-anchor detection, weighted
  soft-signal scoring, union-find, canonical pick.
- `tests/test_paper_dedup_judge.py` — DeterministicJudge, LLMJudge
  caching and prompt_hash invalidation, CompositeJudge borderline
  routing, per-run call budget.
- `tests/test_paper_dedup_ops.py` — `resolve_paper_id` chain/
  cycle/reject handling, `apply_merge` user-state migration across
  tags/topics/chat/chunks/wiki_sources/citations, `PaperMergeEvent`
  snapshot, undo behavior.
- `tests/test_paper_dedup_api.py` — suggestions cache, merge/reject/
  undo endpoints.
- `tests/test_migrate_paper_dedup.py` — one-shot script behavior.

## Validation

```bash
.venv/bin/python -m pytest tests/test_paper_dedup.py tests/test_paper_dedup_judge.py tests/test_paper_dedup_ops.py tests/test_paper_dedup_api.py tests/test_migrate_paper_dedup.py -q
```

## Evidence

- Pipeline: `carrel/pipeline/paper_dedup.py`.
- Merge operations: `carrel/pipeline/paper_dedup_ops.py`.
- LLM judge: `carrel/pipeline/paper_dedup_judge.py`.
- API: `carrel/api/paper_dedup.py`.
- Script: `scripts/migrate_paper_dedup.py`.
- Tables: `PaperAlias`, `PaperDedupVerdict`, `PaperMergeEvent` in
  `carrel/models.py`
  ([../architecture/data-model.md](../architecture/data-model.md)).
- Shared normalization: `carrel/sources/merge.py` (`_clean_doi`,
  `_strip_arxiv_version`, `_normalize_title`).
- Scholar-level equivalent:
  [scholar-dedup.md](scholar-dedup.md).
