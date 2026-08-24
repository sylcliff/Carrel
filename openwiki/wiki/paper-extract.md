---
type: wiki_pipeline
title: Per-paper concept and question extraction
description: LLM extraction of grounded technical concepts and open questions from parsed paper bodies into paper_concepts/paper_questions tables, the input feed for the concept and question wiki compilers.
tags: [wiki, extraction, concepts, questions, evidence, llm]
---

# Per-paper concept and question extraction

`carrel/pipeline/paper_extract.py` is the foundation for the concept
and question wiki layers. For each parsed paper, an LLM pulls out
technical **concepts** and **open questions** that are grounded in
verbatim spans of the paper's body. The outputs are two flat,
compound-PK tables — `paper_concepts` and `paper_questions` — that the
[concept/question compilers](compilers.md) aggregate into pages.

## Extraction scope

The full body is **not** sent: the LLM call is the bottleneck and the
gain from extra context is small once the abstract, introduction, and
conclusion are included. Section selection uses MinerU's preserved ATX
<!-- openwiki: broken internal link [../enrichment/embeddings.md#chunker-carrelchunkingpy] heading anchor "chunker-carrelchunkingpy" does not exist in "../enrichment/embeddings.md". Fix the href or restore the target, then delete this comment. -->
headings (see [`chunking.split_by_heading`](../enrichment/embeddings.md#chunker-carrelchunkingpy)):

- Default: first 2 + last 2 heading sections (`_DEFAULT_HEAD=2`,
  `_DEFAULT_TAIL=2`).
- `deep=True`: first 5 + last 5.
- Fallback when there are no headings: first/last
  `_FALLBACK_HEAD_CHARS=1500` / `_FALLBACK_TAIL_CHARS=1500` characters.
- Total input capped at `_MAX_INPUT_CHARS = 8000`; papers shorter than
  `_MIN_BODY_CHARS = 200` are skipped.

## Concepts

Each concept is assigned one of five categories (the system prompt
enumerates them):

- `METHOD` — a concrete technique/algorithm/model.
- `THEORY` — a theoretical framework, equation, or formal model.
- `DATASET` — a named corpus/benchmark/database.
- `DOMAIN` — a research area more specific than the paper's field.
- `PHENOMENON` — a specific physical effect or observed result.

The LLM returns a full form in `term` (e.g. "density functional
theory") and optional `aliases` (e.g. `["DFT"]`). The row stores
`term_normalized` (lowercased, punctuation-collapsed compound key) and
`term_display` (the most common surface form across papers), plus the
`category` and a verbatim `evidence_quote` from the supplied body.

## Questions

Each open question is stored as
`(paper_id, question_normalized, question_display, evidence_quote)`.
Field-level questions are deferred; v1 extracts per-paper questions
only. Question wiki pages default to `question_status="open"` (see
[compilers.md](compilers.md)).

## Quote verification

Every concept/question must carry a verbatim span from the body that
grounds it. Before write, the pipeline verifies the quote against the
supplied text; **hallucinated mentions (quotes that don't appear) are
dropped silently**, leaving a partial result rather than blocking the
rest of the paper. This is the main integrity guarantee for the wiki's
provenance layer — a `WikiSource.quote` always traces to real text.

## Data model

```
paper_concepts(
  paper_id        FK -> papers.id,
  term_normalized VARCHAR(200),
  term_display    VARCHAR(300),
  evidence_quote  TEXT,
  category        VARCHAR(32),   -- METHOD/THEORY/DATASET/DOMAIN/PHENOMENON
  created_at      TIMESTAMPTZ,
  PRIMARY KEY (paper_id, term_normalized)
)

paper_questions(
  paper_id             FK -> papers.id,
  question_normalized  VARCHAR(400),
  question_display     VARCHAR(600),
  evidence_quote       TEXT,
  created_at           TIMESTAMPTZ,
  PRIMARY KEY (paper_id, question_normalized)
)
```

The compound keys mean a paper's "Retrieval-Augmented Generation"
mention is stored once even if the LLM surfaced it from multiple
sections. `category` is nullable — rows extracted before the category
column existed have `category=None` and downstream consumers treat
None as "unknown".

## Idempotency and staleness

- `extract_paper(session, cfg, paper_id, force=False, deep=False)`
  skips papers that already have extractions unless `force=True`.
- Staleness is `paper.updated_at >
  max(paper_concepts ∪ paper_questions).created_at`; there is no
  per-paper queue column. A re-parse of the paper therefore triggers a
  fresh extraction on the next batch.
- `select_stale_extract(session, limit, deep=False)` returns parsed
  papers whose extractions are missing or stale, for batch runs.
- Failures raise `PaperExtractError` and are caught per-paper by the
  batch driver; the paper's `status` is never touched and existing
  extraction rows are preserved.

## Entry points

- `POST /papers/extract` in `carrel/api/paper_extract.py` — one
  `paper_extract` Job per paper, supports a specific `paper_id`, a
  `limit`, `force`, `deep`, and inline/background execution like the
  other per-paper endpoints.
- `paper_extract` is also the **first stage** of the
  [`POST /wiki/compile` four-stage pipeline](compilers.md#post-wikicompile--four-stage-pipeline),
  so running a full wiki compile refreshes extractions before
  recompiling scholar/concept/question pages.

## Focused tests

- `tests/test_paper_extract.py` — section picking (head/tail),
  fallback windows, `deep` flag, quote verification dropping
  hallucinated spans, category assignment, compound-PK idempotency,
  staleness, non-fatal failure.

## Validation

```bash
.venv/bin/python -m pytest tests/test_paper_extract.py -q
```

## Evidence

- Pipeline: `carrel/pipeline/paper_extract.py`.
- API: `carrel/api/paper_extract.py`.
- Tables: `PaperConcept`, `PaperQuestion` in `carrel/models.py`
  ([../architecture/data-model.md](../architecture/data-model.md)).
- Downstream: [compilers.md](compilers.md),
  [overview.md](overview.md#provenance-wikisource).
- Chunking helper reused for section picking:
  `carrel/chunking.py`
  ([../enrichment/embeddings.md](../enrichment/embeddings.md)).
