---
type: domain_page
title: Papers, library, and user annotations
description: The /papers API surface (list filters/sorts, inbox import/discard, hard delete with cleanup and disk-safety guard, markdown serving) and the favorites/notes/tags annotations router.
tags: [papers, library, inbox, annotations, tags, notes, favorites]
---

# Papers, library, and user annotations

The `Paper` row is the central entity in Carrel. This page covers the API
surface around it — listing/filtering, the inbox-vs-library lifecycle, hard
delete with explicit cleanup, Markdown serving, and user annotations. The
paper state machine itself lives on
[../architecture/data-model.md](../architecture/data-model.md); the sync
producer side lives on [../ingestion/sync.md](../ingestion/sync.md).

## List and filter (`GET /papers`)

`carrel/api/papers.py:list_papers` accepts:

| Param | Default | Behavior |
|---|---|---|
| `limit` / `offset` | 50 / 0 | `limit` capped at 500. |
| `status` | — | Exact `PaperStatus` match. |
| `venue` | — | Case-insensitive ILIKE substring on `Paper.venue`. |
| `in_library` | `true` | `true` = library only; `false` = inbox (auto-adds `discarded=False`); `null` = everything. |
| `favorite` | — | Boolean filter. |
| `tag` | — | Repeatable (`?tag=a&tag=b`); papers matching ANY tag (subquery on `paper_tags` joined to `tags`). |
| `topic` | — | Repeatable; ANY-of semantics like tags. |
| `q` | — | ILIKE substring on title OR the `authors` JSON column (cast to string). |
| `sort` | `added` | One of `added`, `updated`, `pub_newest`, `pub_oldest`, `citations`, `title_az`, `title_za`, `favorites`. Unknown values fall back to `added`. Nulls sort last for date/citation sorts. |

Tags and topics are loaded in two batched queries (`_load_tags_map`,
`_load_topics_map`) rather than per row, avoiding N+1 on list views. Each
row is returned as a `PaperSummary` (the compact projection) with tags and
topics folded in as string lists.

## Inbox vs library

Sync **never** inserts a paper directly into the library. Every new
candidate is written with `in_library=False`, `discarded=False`, and a
`discovered_at` timestamp (see `pipeline.runner.upsert_records`). The user
then:

- `POST /papers/{id}/import` — sets `in_library=True`, `discarded=False`,
  bumps `updated_at`. Importing is idempotent and revives a discarded inbox
  row. Metadata-only papers remain `status=pending`; download/parse is a
  separate step.
- `POST /papers/{id}/discard` — only valid for inbox papers. Sets
  `discarded=True`; library papers must be hard-deleted instead (the route
  returns 409). A later sync leaves the `discarded` flag intact; only an
  explicit import revives it.

Sync refreshes metadata on existing rows (inbox or library) but never flips
`in_library`/`discarded`. Cross-id dedup at sync time can attach a new DOI
or arXiv id to an existing row but never changes membership.

## Hard delete (`DELETE /papers/{id}`)

`delete_paper` removes a paper outright. Invariants:

1. **Explicit child cleanup.** The `chunks` FK has no `ON DELETE CASCADE`,
   so the route deletes every `Chunk` for the paper itself. It also deletes
   `PaperTag` and `PaperTopic` association rows. `ChatMessage` rows,
   `WikiSource` rows, and `PaperMergeEvent` rows rely on `ON DELETE CASCADE`
   declared on the model.
2. **Disk-safety guard.** Before deleting files it resolves the on-disk
   directory from `paper.pdf_path` / `paper.md_path`, then checks
   `resolved_dir.is_relative_to(papers_root)`. Any path that escapes
   `papers_root` (e.g. a poisoned `pdf_path`) is skipped with a logged
   warning — deletion never reaches outside the paper storage subtree.
3. **Best-effort FS cleanup.** The directory is removed with
   `shutil.rmtree(..., ignore_errors=True)`; a missing file must not block
   the DB commit.
4. The `Paper` row itself is deleted last.

This is deliberately not a soft-delete: merged duplicates use the
`status=merged` indirection on the papers table (see
[../dedup/paper-dedup.md](../dedup/paper-dedup.md)); hard delete is only for
the user explicitly removing a paper.

## Detail and Markdown

- `GET /papers/{id}` returns `PaperDetail`, which extends `PaperSummary`
  with abstract, DOI/arXiv/S2 ids, paths, error, author_list (with A-IDs
  and affiliations), citation timestamps, `notes_markdown`,
  `pdf_files`, `journal_doi`, and created/updated timestamps. The id is
  resolved through `paper_dedup_ops.resolve_paper_id` so a request for a
  merged alias id returns the canonical paper.
- `GET /papers/{id}/markdown` reads the parsed `paper.md` from disk
  (storage-root + `paper.md_path`) and returns it as `text/markdown`. A 404
  is returned when the row or file is missing.

## Favorites, notes, tags (`carrel/api/annotations.py`)

The router has no prefix so it can mix `/papers/{id}/...` routes with the
global `/tags` collection.

- **Favorites** — `POST /papers/{id}/favorite` with `{favorite: bool}`.
  Bumps `updated_at` so favorites sort naturally in recent views.
- **Notes** — `PUT /papers/{id}/notes` with `{notes_markdown: str}`. This is
  a **whole-document PUT**: the body replaces the entire note. Whitespace-only
  input is stored as `None` (i.e. clears the note). There is no PATCH/diff.
  Also bumps `updated_at`.
- **Tags per paper**
  - `GET /papers/{id}/tags` lists the paper's tags.
  - `POST /papers/{id}/tags` with `{name}` calls `_get_or_create_tag`, which
    looks up an existing tag by ILIKE. If one exists it is reused (so
    `"BERT"` and `"bert"` collapse to the first-seen spelling — case is
    preserved from the first use); otherwise a new `Tag` is inserted. The
    route then inserts the `PaperTag` association (the composite PK makes
    repeated adds idempotent).
  - `DELETE /papers/{id}/tags/{tag_id}` detaches the tag from this paper.
- **Global tags**
  - `GET /tags` returns every tag with its paper count.
  - `DELETE /tags/{tag_id}` deletes the tag itself and detaches it from all
    papers. There is no `ON DELETE CASCADE` on `paper_tags.tag_id` in both
    directions, so the route explicitly removes every `PaperTag` row first
    (cascade-detach loop).

## Pydantic shapes

- `PaperSummary` — list/card projection (id, title, venue, date, authors,
  oa_status, status, tldrs, keywords, source, citation_count, in_library,
  discovered_at, favorite, tags, topics).
- `PaperDetail` — adds abstract, ids, paths, error, author_list,
  influential/reference counts, citations_updated_at, notes, pdf_origin,
  journal_doi, pdf_files, published_checked_at, created/updated.
- `FavoriteIn/Out`, `NotesIn/Out`, `TagIn/Out/WithCount`.

## Focused tests

- `tests/test_inbox_api.py` — import/discard lifecycle, library vs inbox
  filters, the 409 when discarding a library paper.
- `tests/test_annotations_api.py` — favorite toggle, whole-body PUT clear
  semantics, case-insensitive tag dedup preserving first-seen casing,
  per-paper tag add/remove, global tag delete with cascade-detach.
- `tests/test_api.py` — generic list/detail smoke.
- `tests/test_paper_dedup_api.py` — merged-id resolution on detail.

## Validation

```bash
.venv/bin/python -m pytest tests/test_inbox_api.py tests/test_annotations_api.py tests/test_api.py -q
```

## Evidence

- Routes: `carrel/api/papers.py`, `carrel/api/annotations.py`.
- Schemas: `carrel/schemas.py` (`PaperSummary`, `PaperDetail`,
  `Favorite*`, `Notes*`, `Tag*`).
- Cascade declarations: `carrel/models.py` (`ChatMessage`, `WikiSource`,
  `PaperMergeEvent` FKs).
- Sync producer: [../ingestion/sync.md](../ingestion/sync.md).
- Frontend consumers: `frontend/src/pages/Library.tsx`,
  `frontend/src/pages/PaperDetail.tsx`,
  `frontend/src/components/NotesCard.tsx`,
  `frontend/src/components/PaperList.tsx`.
