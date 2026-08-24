---
type: wiki_pipeline
title: Wiki identity reconciliation
description: Decouples wiki entity identity (entity_key) from on-disk address (kind, slug), so A-ID assignment, alias merges, and name-spelling changes rewrite pages into redirect shells instead of leaving orphans.
tags: [wiki, identity, entity-key, redirect, reconciliation, aliases]
---

# Wiki identity reconciliation

The wiki catalog is *addressed* by `(kind, slug)`, but a scholar's
*identity* changes over the lifetime of a paper:

- A name-only record acquires an OpenAlex A-ID (via
  [author backfill](../enrichment/authors-backfill.md)).
- An A-ID is found to be a duplicate of another and merged via
  `scholar_aliases` ([scholar-dedup](../dedup/scholar-dedup.md)).
- A Chinese author's name is romanized two different ways.

Without reconciliation the address layer happily writes a new page at
the new slug and leaves the old one orphaned. The identity layer fixes
this by giving every live page a stable `entity_key` and a single
reconciliation pass that converges the catalog to the source of truth.

## `entity_key` shapes

- `scholar:<A-ID>` — e.g. `scholar:A5013214678`.
- `scholar:name:<normalized-name>` — e.g. `scholar:name:he-li` for
  name-only scholars.
- `concept:slug:<slug>` and `question:slug:<slug>` — placeholder keys
  for kinds that don't yet have a richer identity (a future pass can
  replace them).

`entity_key` is independent of slug/path, so identity changes don't
break catalog consumers. A **partial unique index**
`uq_wiki_pages_entity_key_live` enforces exactly one *live* page per
entity:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_wiki_pages_entity_key_live
ON wiki_pages (entity_key)
WHERE redirects_to IS NULL AND entity_key IS NOT NULL
```

Redirect shells are allowed to share an `entity_key` with their
canonical (they are excluded by the predicate). The same DDL works on
both Postgres and SQLite ≥ 3.8 (partial indexes are honored in
`CREATE INDEX` form). A redirect shell is a row whose `redirects_to`
column carries the canonical entity_key (stored as a string, not a
self-FK, so cycles can never produce a 500).

## `reconcile_kind` — `carrel/pipeline/wiki/_entities.py`

The kind-agnostic driver. Each kind declares:

- `enumerate_entities(session) -> Iterable[EntityRef]` — the current
  canonical set, where `EntityRef(entity_key, kind, slug, title,
  extra)`.
- `resolve_alias(session, entity_key) -> canonical_entity_key | None`
  — optional; used when a live page's key no longer appears in the
  enumeration. The scholar kind resolves through `scholar_aliases`;
  other kinds pass a no-op.

For each live page of the kind, `reconcile_kind` handles four cases:

- **Case A — entity moved addresses:** the entity_key is in the
  enumeration but its current `(kind, slug)` differs from the page's.
  `_find_or_open_target` ensures a live row exists at the new address,
  `_retarget_page` moves `WikiSource` rows to it, and
  `_rewrite_file_as_redirect` atomically converts the old on-disk file
  into a redirect shell (`redirects_to: <entity_key>` plus a
  `[[...]]`-style link body). Counters: `rewritten`, `moved_sources`,
  `moved_files`.
- **Case B — page already correct:** no-op (`skipped`).
- **Case C — entity aliased away:** the entity_key is not enumerated
  but `resolve_alias` returns a canonical that is. The row becomes a
  redirect shell and its sources move to the canonical's row.
- **Case D — genuinely orphaned:** no alias matches. The row is left
  in place, a warning is logged, and `unresolved` is incremented so an
  operator can inspect it. Reconciliation never deletes live content.

Redirect chains are bounded by `_MAX_REDIRECT_HOPS = 4` to defend
against cycles introduced by hand edits or buggy auto-merges. The
operation is idempotent — re-running on a converged catalog is a no-op
— and returns a `ReconcileResult` with per-case counters.

## Scholar alias resolver

`scholar_alias_resolver(session, entity_key)` handles:

1. `scholar:<A-ID>` — looks up `scholar_aliases` (ignoring
   `source='reject'`), follows the chain to its canonical A-ID, and
   returns `scholar:<canonical>`.
2. `scholar:name:<name>` — a name-only page whose author later
   acquired an A-ID is resolved by aggregating current library papers
   and finding the A-ID now associated with that normalized name.

## Startup reconciliation in `db.py`

`init_db` runs three steps in order (the order matters):

1. `backfill_wiki_identity(engine)` — populates `entity_key` for
   existing rows that predate the column, deriving
   `scholar:<aid>` / `scholar:name:<base>` /
   `scholar:slug:<normalized title>` via `_derive_entity_key`, and
   `kind:slug:<slug>` as a placeholder for other kinds. Idempotent.
2. `retire_duplicate_wiki_pages(engine)` — for each `entity_key` with
   more than one non-redirect row, picks a canonical (the row whose
   `scholar_aid` matches the aggregated key for that entity, or the
   one with the most `wiki_sources` as a tiebreaker) and converts the
   rest into redirect shells. This runs *before* the partial unique
   index is created, so duplicate pages don't cause index creation to
   fail.
3. `_ensure_wiki_identity_index(engine)` — creates the partial unique
   index `IF NOT EXISTS`.

This same machinery is invoked by the cleanup script
`scripts/cleanup_duplicate_wiki.py`.

## Link resolution on top of identity

`_links.resolve_target` (used by `recompute_backlinks` and the
by-kind-slug lookup) follows up to `_MAX_REDIRECT_HOPS` redirect
shells, with an in-process cache cleared by every reindex entry point
so a stale mapping after an alias merge doesn't leak across runs. The
scholars API looks up a compiled page by `entity_key` first (which
sees through A-ID assignment) and falls back to a slug match for
legacy rows whose entity_key was never populated (see
[../backend/scholars.md](../backend/scholars.md)).

## Invariants

- The on-disk file is the source of truth for the page body; the DB
  row is an index. Reconcile rewrites the disk file, not just the row.
- A merge is reversible at the alias-table level (delete the alias);
  the redirected page can be rebuilt from disk by a reindex.
- `WikiSource` rows are reassigned, never duplicated, so backlink
  counts stay exact.
- Redirect shells never carry an `entity_key` (it is cleared) so the
  unique index allows them to coexist with the canonical.

## Focused tests

- `tests/test_wiki_reconcile.py` — case A/B/C/D behavior,
  `_MAX_REDIRECT_HOPS` cycle guard, alias resolution, source-row
  reassignment, redirect-shell file format, idempotency, and the
  startup backfill/retire/index ordering.
- `tests/test_wiki_reindex.py` — reindex preserving entity_key and
  embeddings, redirect-shell upsert, `clear_resolve_cache` behavior.
- `tests/test_wiki_links.py` — dual-link extraction and redirect
  chasing.
- `tests/test_wiki_merge.py` — protected user-section preservation
  across recompiles (which runs alongside identity moves).

## Validation

```bash
.venv/bin/python -m pytest tests/test_wiki_reconcile.py tests/test_wiki_reindex.py tests/test_wiki_links.py tests/test_wiki_merge.py -q
```

## Evidence

- Driver: `carrel/pipeline/wiki/_entities.py`.
- Name normalization: `carrel/pipeline/wiki/_names.py`.
- Slug/frontmatter/links: `_slug.py`, `_frontmatter.py`, `_links.py`.
- Startup backfill/retire/index: `carrel/db.py`
  (`backfill_wiki_identity`, `retire_duplicate_wiki_pages`,
  `_ensure_wiki_identity_index`); see
  [../backend/database.md](../backend/database.md).
- Alias source: `ScholarAlias` table and
  [../dedup/scholar-dedup.md](../dedup/scholar-dedup.md).
- Catalog/contract: [overview.md](overview.md),
  [compilers.md](compilers.md).
