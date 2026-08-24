---
type: pipeline
title: LLM summarization
description: Bilingual TL;DR and Chinese summary + keyword generation for parsed papers, with fill-missing semantics, litellm-backed model/fallback selection, JSON-only prompt contract, and non-fatal failure handling.
tags: [llm, summarization, tldr, keywords, litellm]
---

# LLM summarization

`carrel/pipeline/summarize.py` generates four fields on a parsed
`Paper`:

- `tldr_en` — one sentence in English (≤ 40 words).
- `tldr_zh` — one sentence in Simplified Chinese (≤ 40 characters).
- `summary_zh` — 3–5 sentence Chinese abstract.
- `keywords` — 5–8 English technical keywords as a JSON array.

The state transition is `parsed → summarized`, but the step is strictly
**non-fatal**: on failure the paper stays `parsed` so embedding (which
accepts `parsed`, `summarized`, and previously `failed` rows) can still
run, and `paper.error` is not overwritten (so a successful parse is not
obscured). `summarize_paper` never writes the status backwards — an
already-`ready` paper is not regressed to `summarized` or `failed` by a
re-run, and on success it only advances `parsed → summarized`.

## Prompt and body preparation

The system prompt instructs the model to:

- Base every claim only on the supplied paper text.
- Return **only** a JSON object of the required shape — no markdown
  fences, no prose.
- Truncate the parsed Markdown to `cfg.llm.max_input_chars` (default
  12 000) after stripping image markup via `_prepare_body`.
  `_prepare_body` is also reused by the wiki compilers and the chat
  full-text fallback.

The user prompt includes title, authors, venue/date, abstract (when
present), and the truncated body.

## Fill-missing semantics

`summarize_paper(session, cfg, paper_id, force=False, on_progress=None)`:

- Loads the Paper and reads the Markdown at
  `<storage.root>/<paper.md_path>`.
- When a field is already populated (e.g. `tldr_en` was sourced from
  Semantic Scholar during citation enrichment) it is **preserved**
  unless `force=True`; only missing fields returned by the model are
  written. This is the "fill-missing" contract that makes S2 tldrs
  survive re-summarization.
- Validates the JSON response and applies fields in the order declared
  by `_OUTPUT_FIELDS`.
- On success sets `status='summarized'`; on `SummarizeError` the status
  is left at `parsed` (or `summarized` if it was already) and the
  exception is re-raised so the wrapping Job is marked failed with the
  reason (e.g. missing API key, parse error, file missing).

## LLM transport

`carrel/llm.py` exposes `chat_json(messages, *, model, fallback_model,
temperature, timeout, max_retries)`:

- Resolves the API key from the environment via
  `embeddings._key_for(model)` (the same provider-prefix → env-var map
  used for embeddings). Provider prefixes supported: `volcengine`,
  `deepseek`, `openai`.
- Builds an attempt list `[(model, key)]` plus `(fallback_model,
  fallback_key)` when a key exists for it. A primary with no key falls
  straight through to the fallback.
- Calls litellm's `completion` with `response_format={"type": "json_object"}`
  and retries transient errors (429/5xx/timeout) with backoff
  (`DEFAULT_MAX_RETRIES = 3`).
- Parses `resp.choices[0].message.content` as JSON; raises `LLMError`
  when no model produces a valid JSON object. All failures across both
  attempts normalize to `LLMError` so callers catch one type.
- Imports litellm lazily so tests run without the dependency or any
  keys installed.

`carrel/llm.py` also exposes `chat_stream(...)` for the RAG chat
endpoint (token-by-token generator with the same model/fallback
resolution), and `has_key_for(model)` for cheap pre-flight checks used
by `/health` and compiler stubs.

## Idempotency and batch selection

- `select_pending_summarize(session, limit)` returns parsed papers
  missing at least one output field (S2 tldr present is fine; the other
  three still need filling).
- Re-running on an already-summarized paper is a no-op unless `force`.
- The summarize Job is created per-paper by `POST /summarize`
  (`carrel/api/summarize.py`), mirroring `/process` and `/embed`; the
  `force` flag is only honored when `paper_id` is given (batch never
  forces).

## Chaining

`process_paper` calls `summarize_paper` best-effort immediately after a
successful MinerU parse (see
[../ingestion/pdf-processing.md](../ingestion/pdf-processing.md)). The
`/summarize` endpoint exists to (re)run the step independently, e.g.
after adding an API key.

## Focused tests

- `tests/test_summarize_pipeline.py` — body truncation, fill-missing
  preservation (including S2 tldr), non-fatal failure, force rerun,
  JSON parse errors.
- `tests/test_summarize_api.py` — job creation, batch selection,
  per-paper force flag, background/inline execution.

## Validation

```bash
.venv/bin/python -m pytest tests/test_summarize_pipeline.py tests/test_summarize_api.py -q
```

## Evidence

- Pipeline: `carrel/pipeline/summarize.py`.
- LLM transport: `carrel/llm.py`.
- Config: `cfg.llm.summarize_model`, `fallback_model`,
  `summarize_provider`, `temperature`, `request_timeout_seconds`,
  `max_input_chars` (see
  [../architecture/configuration.md](../architecture/configuration.md)).
- API: `carrel/api/summarize.py`.
- Downstream: [embeddings.md](embeddings.md),
  [../wiki/paper-extract.md](../wiki/paper-extract.md) (uses
  `_prepare_body`), [../backend/search-and-chat.md](../backend/search-and-chat.md)
  (uses `chat_stream`).
