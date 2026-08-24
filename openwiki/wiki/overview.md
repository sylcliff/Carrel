---
type: wiki_overview
title: LLM wiki layer
description: The compiled Markdown wiki (scholar/concept/question pages) on disk, its wiki_pages/wiki_sources index, frontmatter/link/slug conventions, and the reindex/backlink rebuild that makes disk the source of truth.
tags: [wiki, markdown, frontmatter, wikilinks, reindex, provenance]
---

# LLM wiki layer

The wiki is a layer of *compiled* Markdown pages synthesized from the
library's papers, sitting above the immutable chunk store. It is the
surface behind the **Wiki** and **Scholars** sections of the UI.

- **Source of truth on disk:** `data/wiki/{concepts,scholars,questions}/*.md`
  (under `cfg.storage.root`). Each page is a Markdown file with YAML
  frontmatter.
- **Rebuildable index in the DB:** `wiki_pages` mirrors frontmatter and
  checksum for list/filter views without file IO; `wiki_sources` is the
  per-assertion provenance map back to papers/chunks.
- **Three kinds:** `scholar` (researcher profile pages, keyed by
  OpenAlex A-ID or normalized name), `concept` (recurring technical
  terms), `question` (open research questions). Concepts and questions
  are fed by per-paper LLM extraction
  ([paper-extract.md](paper-extract.md)); scholars are aggregated
  directly from `Paper.authors`.

## Frontmatter (`_frontmatter.py`)

A page is `---\n<yaml>\n---\n<body>`. Parsing is line-based rather than
a full Markdown parser; a malformed block returns `({}, text)` so a bad
hand edit never destroys the body. The compilers write:

- `title`, `summary`, `tags`
- Scholar pages: `scholar_aid`, `entity_key` (e.g.
  `scholar:A5013214678` or `scholar:name:he-li`)
- Question pages: `question_status` (`open` in v1; reserved for
  `contested`/`partially_solved`/`resolved`)
- Redirect shells: `redirects_to: <entity_key>`
- `confidence`, `evidence_count`, `stub`, `sources` (list of paper
  ids/footnote markers)

The DB additionally mirrors `links_out`, `checksum` (sha256 of file
bytes), `links_in_count`, `source_paper_ids`, `compiled_at`, and an
`embedding` (halfvec, 2048 dims) used for wiki semantic navigation.

## Wikilinks (`_links.py`)

Internal references use a **dual format** that both Obsidian and a
plain Markdown viewer understand:

```
[[Display label]](../concepts/foo.md)
```

Obsidian sees the `[[...]]`; standard Markdown follows the `(...)`
relative URL. The frontend rehype plugin (`rehypeWikiLinks.ts`) turns
these into client-side routes. External links (`http(s)://`,
`/papers/...`, `mailto:`, `#anchor`) are left alone.

`extract_wikilinks(md)` returns `(display, href)` pairs;
`resolve_link(from_path, href)` maps an href to a target
`(kind, slug)` and then to a `WikiPage.id`. `resolve_target` follows up
to `_MAX_REDIRECT_HOPS = 4` redirect shells, with an in-process cache
that is cleared by reindex entry points. `recompute_backlinks(session)`
re-counts incoming links for every page.

## Slugs (`_slug.py`)

- A-IDs are used verbatim as slugs (`A5013214678`) — always `A` +
  digits, so they can never collide with a name slug.
- Name-only scholars get `name--<normalized-name>` (e.g.
  `name--he-li`).
- Concepts/questions use ASCII-lowercased, dash-collapsed slugs.
- `page_path(kind, slug)` returns the storage-root-relative path,
  e.g. `wiki/scholars/A5013....md`. Each kind has its own plural
  directory on disk (`concepts/`, `scholars/`, `questions/`) even
  though the enum value is singular.

## Reindex (`_reindex.py`)

`reindex_wiki(session, cfg)` walks every `*.md` under
`<storage>/wiki/{concepts,scholars,questions}` (skipping `_index.md`),
parses frontmatter, and upserts a `WikiPage` row:

- Slug from the filename, kind from the directory.
- Frontmatter mirrors (summary, tags, links_out, source_paper_ids,
  scholar_aid, question_status, entity_key, redirects_to, confidence,
  evidence_count, stub).
- `checksum` recomputed from file bytes; embeddings are **left intact**
  (they are keyed by page id).
- Recognizes a `redirects_to` frontmatter key and turns the row into a
  redirect shell (clears content mirrors; does not carry an
  `entity_key`).
- Files whose rows no longer exist are **left in place** (they may be
  mid-compile); `prune_dead_links` is the separate cleanup pass.

Returns `{indexed, files_seen}`. The reindex is invoked:

- From `scholar_compile.reindex_and_seed_scholars` after a batch of
  scholar pages is written.
- From the startup wiki-identity reconciliation
  ([reconciliation.md](reconciliation.md)).
- On demand via the cleanup script.

`prune_dead_links(session)` removes `WikiSource` rows whose page or
paper/chunk no longer exists, and deletes `WikiPage` rows whose file
has been removed.

## Identity vs address

The catalog is *addressed* by `(kind, slug)`, but identity is the
`entity_key` (stable across A-ID assignment, alias merges, and
name-spelling changes). A partial unique index
`uq_wiki_pages_entity_key_live` enforces one live page per entity
(redirect shells share an `entity_key` with their canonical and are
excluded by the `WHERE redirects_to IS NULL` predicate). Identity
reconciliation is documented separately on
[reconciliation.md](reconciliation.md).

## Provenance (`WikiSource`)

Each compiled page records its evidence as `WikiSource` rows:

- Scholar pages cite at the paper/abstract level (`chunk_id=NULL`).
- Concept and question pages pin claims to a specific chunk
  (`chunk_id`, `heading`, `quote`) with a `role` of `support`,
  `contradict`, or `context` for questions.

`WikiSource` has `ON DELETE CASCADE` to both `wiki_pages` and
`papers`, so deleting a paper cleans up its sources automatically (and
`delete_paper` in the API does not need to enumerate them).

## API

`carrel/api/wiki.py` exposes:

- `GET /wiki/pages` — filterable list (kind, stub, q, confidence
  floor, tags), summary schema only.
- `GET /wiki/pages/{id}` and
  `GET /wiki/pages/by-kind-slug/{kind}/{slug}` — full detail including
  body, sources (resolved to paper titles), and backlinks.
- `POST /wiki/compile` — kick off the multi-stage batch Job (see
  [compilers.md](compilers.md)).
- `POST /wiki/pages/{id}/recompile` — force-recompile one page.

## Focused tests

- `tests/test_wiki_frontmatter.py` — fence parsing, malformed block
  tolerance.
- `tests/test_wiki_slug.py` — A-ID vs name-prefix disambiguation.
- `tests/test_wiki_links.py` — dual-link extraction and internal
  classification.
- `tests/test_wiki_merge.py` — protected user-section merge.
- `tests/test_wiki_reindex.py` — upsert/redirect/checksum/backlink
  behavior.
- `tests/test_wiki_reconcile.py` — identity moves and redirect-shell
  conversion.
- `tests/test_wiki_api.py` — list/detail/by-kind-slug/compile/
  recompile endpoints.

## Validation

```bash
.venv/bin/python -m pytest tests/test_wiki_frontmatter.py tests/test_wiki_slug.py tests/test_wiki_links.py tests/test_wiki_merge.py tests/test_wiki_reindex.py tests/test_wiki_reconcile.py tests/test_wiki_api.py -q
```

## Evidence

- Package docstring: `carrel/pipeline/wiki/__init__.py`.
- Index/links/slug/frontmatter/merge:
  `carrel/pipeline/wiki/{_reindex,_links,_slug,_frontmatter,_merge,_names}.py`.
- Tables: `WikiPage`, `WikiSource` in `carrel/models.py`.
- API: `carrel/api/wiki.py`.
- Compilers: [compilers.md](compilers.md).
- Extraction feed: [paper-extract.md](paper-extract.md).
- Identity reconciliation: [reconciliation.md](reconciliation.md).
- Frontend: `frontend/src/pages/WikiIndex.tsx`, `WikiPageList.tsx`,
  `WikiPageDetail.tsx`, and the `rehypeWikiLinks.ts` rehype plugin.
