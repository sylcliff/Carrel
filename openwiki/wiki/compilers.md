---
type: wiki_pipeline
title: Wiki compilers
description: The three LLM compilers (scholar, concept, question), their shared aggregate→prompt→atomic-write→reindex pattern, the scheduled scholar-only job, and the four-stage /wiki/compile batch with its no-op skip gate.
tags: [wiki, compiler, scholar, concept, question, llm, batch]
---

# Wiki compilers

The wiki has three LLM compilers, one per `WikiKind`:

- `scholar_compile.compile_scholar` — researcher profile pages
  synthesized from in-library paper **metadata and abstracts/tldrs
  only** (the parsed PDF is never read, so metadata-only papers are
  fully covered and the LLM input stays small).
- `concept_compile.compile_concept` — pages for recurring technical
  terms, grounded in `PaperConcept` extractions
  ([paper-extract.md](paper-extract.md)).
- `question_compile.compile_question` — pages for open research
  questions, grounded in `PaperQuestion` extractions.

All three live under `carrel/pipeline/wiki/` and share the same shape
(aggregate candidate papers → build prompt → call LLM → render Markdown
→ atomic write → upsert `WikiPage` + `WikiSource` rows → embed).

## Shared compile pattern

### Candidate aggregation

- Scholars: `_scholars_agg.aggregate(session)` groups authors by
  OpenAlex A-ID (resolved through `ScholarAlias`), falling back to
  `name:<normalized-name>`. `papers_for_key` returns that scholar's
  in-library papers newest first.
- Concepts: `_concepts_agg.aggregate(session)` groups `PaperConcept`
  rows by `term_normalized`. `papers_for_term` returns the contributing
  papers with their evidence quotes. `EVIDENCE_THRESHOLD` (default 3)
  controls when a concept gets a full LLM page vs a stub.
- Questions: `_questions_agg` is the structural twin for
  `PaperQuestion`, with its own `EVIDENCE_THRESHOLD`.

### Staleness and stubs

- Each `compile_*_pending(session, cfg, limit, force, on_progress)`
  picks entities whose newest supporting paper has `paper.updated_at >
  wiki_page.compiled_at`, so a scholar/concept/question with no newer
  evidence is a no-op. There is no per-paper queue column; staleness is
  derived from timestamps.
- Entities below `EVIDENCE_THRESHOLD` get a **stub page**: the LLM is
  skipped and a placeholder body is written. The row is marked
  `stub=True` so list views can filter it out; when enough evidence
  accumulates, the next compile promotes it to a full page.

### Prompt and rendering

- `_SYSTEM_PROMPT` in each module demands JSON-only output
  (`{"summary": ..., "tags": ..., "confidence": ...}` plus
  kind-specific fields: scholars return `research_lines`,
  `trajectory`, `evolving_views`, `key_collaborators`; questions
  return `why_it_matters` and carry a `question_status` defaulting to
  `"open"`).
- The rendered Markdown body has a `# Title`, `## Summary`, sources
  list with footnote markers `[^n]` that map to `WikiSource` rows, and
  (for scholars only) a protected
  `<section data-user="true">…</section>` block preserved verbatim
  across recompiles via `_merge.py`.
- Input is capped (`_MAX_PAPERS = 25`, `_MAX_INPUT_CHARS` between
  8 000 and 10 000, abstracts/tldrs trimmed with
  `summarize._prepare_body`).

### Atomic write, idempotency, embedding

- Each page is rendered to a temp file in the same directory and
  `os.replace`d into place, so a crash never leaves a half-written
  page.
- The compiler hashes the rendered body; if it matches the previous
  content the file is not rewritten and the LLM call is skipped on the
  next staleness check.
- After writing, the first `_EMBED_CHARS` (1500) of the body are
  embedded via `carrel.embeddings.embed_texts` and stored on
  `WikiPage.embedding` as `halfvec(2048)`.
- `reindex_and_seed_scholars` (scholar-specific) calls
  `_reindex.reindex_wiki` after a batch so the DB index matches disk;
  the other compilers rely on the final reindex pass in the batch
  driver.

## Two batch entry points

### Scheduled job — scholars only

`carrel/scheduler.py::_scheduled_wiki_compile` calls
`compile_scholars_pending(session, cfg, limit, on_progress)` and
nothing else. It is the body of the `wiki_compile` `JOB_SPECS` entry
(default cron `17 11 * * *`, disabled). Scholars compile cheaply from
metadata; concept/question compilation requires the heavier
`paper_extract` step and is not run on a timer by default.

### `POST /wiki/compile` — four-stage pipeline

`carrel/api/wiki.py::_run_batch` runs an ordered, stage-isolated
pipeline:

```
paper_extract → scholar_compile → concept_compile → question_compile
```

Behavior:

- `_ALL_STAGES` defines the order; the request body's `stages` filter
  can select a subset. Unknown stage names fail the Job with
  `"unknown stages: [...]"`.
- Each stage is wrapped in its own `try/except`: a crash in one stage
  is recorded under `Job.stats[<stage>].error` and does **not** roll
  back earlier stages.
- `_stage_did_work(stage, counts)` returns False when no live compile
  happened **and** there were no failures. Stubbed pages count as
  no-op (no LLM call). When a stage is a no-op, every subsequent stage
  is skipped with `{"skipped": true, "reason": "prev_noop"}`, under
  the assumption that the input didn't change and downstream staleness
  is unchanged.
- Per-stage counts nest under `Job.stats[<stage>]`; the top-level
  `stats.stage` reports the most-recent phase for the UI badge, and
  `job.message` carries `[i/total] <name> — <detail>` progress.
- After the stages, the final pass calls
  `_reindex.prune_dead_links(session)` then
  `_reindex.recompute_backlinks(session)`. It does **not** call
  `reindex_wiki` (that is a separate disk-to-index rebuild used by
  `reindex_and_seed_scholars` and the startup wiki-identity
  reconciliation — see [overview.md](overview.md#reindex-_reindexpy)).
- The Job is `kind='wiki_compile'` and supports the usual
  `background=true` (default) / inline modes.

`POST /wiki/pages/{page_id}/recompile` runs a single page through the
matching compiler with `force=True`, wrapped in its own
`wiki_recompile` Job.

## Focused tests

- `tests/test_scholar_compile.py` — metadata-only aggregation,
  protected user-section merge, atomic write, hash-skip idempotency,
  embedding, reindex.
- `tests/test_concept_compile.py` — `EVIDENCE_THRESHOLD` stub vs full
  page, evidence quote grounding, category handling.
- `tests/test_question_compile.py` — question_status default, stub
  threshold, why_it_matters field.
- `tests/test_wiki_api.py` — `/wiki/pages` list/detail/by-kind-slug,
  `/wiki/compile` four-stage execution, no-op skip gate, stage
  filtering, per-stage error isolation, `/recompile`.

## Validation

```bash
.venv/bin/python -m pytest tests/test_scholar_compile.py tests/test_concept_compile.py tests/test_question_compile.py tests/test_wiki_api.py -q
```

## Evidence

- Compilers: `carrel/pipeline/wiki/{scholar,concept,question}_compile.py`.
- Aggregators: `_scholars_agg.py`, `_concepts_agg.py`,
  `_questions_agg.py`.
- Shared helpers: `_merge.py` (user section), `_reindex.py`,
  `_slug.py`, `_frontmatter.py`, `_links.py`.
- Batch driver and endpoints: `carrel/api/wiki.py`.
- Scheduled scholar-only job: `carrel/scheduler.py`
  (`_scheduled_wiki_compile`); see
  [../architecture/scheduler-and-jobs.md](../architecture/scheduler-and-jobs.md).
- Input feed: [paper-extract.md](paper-extract.md).
- Index/link contracts: [overview.md](overview.md).
