---
type: pipeline
title: Topic classification
description: LLM classifier that assigns 1-4 broad, reusable research topics to in-library papers from metadata only, growing a shared Topic vocabulary via many-to-many PaperTopic rows.
tags: [topics, llm, classification, paper-topics, vocabulary]
---

# Topic classification

`carrel/pipeline/topics.py` classifies a paper into 1–4 broad,
human-readable research themes (e.g. "LLM Agents",
"Retrieval-Augmented Generation") using **metadata only** — title,
authors, venue/date, abstract, keywords, and source categories cached
in `raw_meta`. It does not require a parsed PDF, so metadata-only
library papers can be classified before PDF processing.

The classifier is given the names of every existing `Topic` and told to
reuse one verbatim when it fits, inventing a new canonical name only
when nothing matches. This makes topics a **shared, browsable
vocabulary** that grows organically with the library, distinct from
per-paper free-form keywords and from user `Tag`s.

## Data model

- `Topic(id, name UNIQUE, description, created_at)` — the shared
  vocabulary. New names must be short Title-Case noun phrases in
  English (the system prompt enforces this; no trailing punctuation).
- `PaperTopic(paper_id, topic_id)` — many-to-many association with a
  composite primary key and an index on `topic_id` for reverse lookups.

There are no foreign-key cascade concerns unique to topics; deleting a
paper explicitly removes its `PaperTopic` rows (see
[../backend/papers-and-library.md](../backend/papers-and-library.md)).

## Classification

`topics_paper(session, cfg, paper_id, force=False)`:

1. Skips papers that already have topics unless `force=True`.
2. Builds the user prompt from metadata + existing topic names.
3. Calls `llm.chat_json` with the configured summarizer model (the
   classifier reuses `cfg.llm.summarize_model` / fallback).
4. Validates the response: `{"topics": [{"name": "...", "description":
   "..."}]}` with 1–4 entries. New names are upserted into `Topic`
   (description is filled in only when the topic is first created);
   existing names are reused case- and whitespace-insensitively via
   `_get_or_create_topic`. Integrity errors from a concurrent insert
   fall back to selecting the existing row.
5. Replaces the paper's `PaperTopic` set atomically (delete-then-add)
   inside the same transaction.

Failures raise `TopicsError`; the paper's `status` and `error` are
never touched, so embedding/search are unaffected.

## Idempotency and batch selection

- `select_pending_topics(session, limit)` returns in-library,
  non-discarded papers with no `PaperTopic` rows.
- The batch Job (one Job per paper) is created by `POST /topics`
  (`carrel/api/topics.py`), mirroring `/summarize`. `GET /topics`
  lists every topic with the count of papers carrying it, for the
  sidebar facet and the Topics browse page.

## Focused tests

- `tests/test_topics_pipeline.py` — reuse-vs-invent behavior,
  1–4 bounds, name normalization, idempotency, non-fatal failure,
  concurrent-insert integrity fallback.
- `tests/test_topics_api.py` — `POST /topics` batch and
  `GET /topics` counts.

## Validation

```bash
.venv/bin/python -m pytest tests/test_topics_pipeline.py tests/test_topics_api.py -q
```

## Evidence

- Pipeline: `carrel/pipeline/topics.py`.
- API: `carrel/api/topics.py`.
- Models: `Topic`, `PaperTopic` in `carrel/models.py`.
- LLM transport: `carrel/llm.py` (see
  [summarization.md](summarization.md)).
- Distinctions: user tags live in `Tag`/`PaperTag`
  ([../backend/papers-and-library.md](../backend/papers-and-library.md));
  the wiki's `PaperConcept` is a per-paper LLM extraction of finer
  technical terms ([../wiki/paper-extract.md](../wiki/paper-extract.md)).
- Frontend: `frontend/src/pages/Topics.tsx`,
  `frontend/src/components/TopicSidebar.tsx`.
