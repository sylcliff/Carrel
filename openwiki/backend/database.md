---
type: database
title: Database engine, sessions, and bootstrap
description: make_engine, pgvector registration, init_db with additive column migrations and HNSW indexes, wiki-identity reconciliation, the session dependency, and the SQLite test fallback.
tags: [database, sqlmodel, pgvector, sqlite, migrations, hnsw]
---

# Database engine, sessions, and bootstrap

`carrel/db.py` owns engine creation, schema bootstrap, the FastAPI session
dependency, and the wiki-identity reconciliation that runs on every startup.

## Engine creation

`make_engine(database_url)` (`carrel/db.py:19`) creates a SQLAlchemy engine
with `pool_pre_ping=True`. On PostgreSQL it registers a `connect` event
listener that runs `CREATE EXTENSION IF NOT EXISTS vector;` on every new
connection, so pgvector is available even against a fresh database. There is
no connection-pool tuning beyond SQLAlchemy defaults — Carrel is single-user
and single-process.

The app engine is created once in lifespan by `init_app_engine(env)` and
stored in a module global accessed by `get_app_engine()`. Background tasks
and scheduler threads call `get_app_engine()` to open their own
`Session(engine)`; request handlers use the FastAPI dependency
`get_session_dep()` instead.

## Schema bootstrap

`init_db(engine)` runs on every startup and is idempotent:

1. `CREATE EXTENSION IF NOT EXISTS vector` on Postgres.
2. `SQLModel.metadata.create_all(engine)` — creates every missing table.
   `create_all` never alters an existing table, so new columns on
   already-released tables are handled by step 3.
3. `_ensure_columns(engine, table, columns)` — additive ALTER TABLE ADD
   COLUMN for a hard-coded set of post-release columns on `papers`,
   `wiki_pages`, and `paper_concepts`. Each statement is dialect-quoted,
   checks `inspector.get_columns`, and is safe to re-run.
4. Backfills:
   - `papers.in_library=TRUE` for rows where it is NULL (inbox feature
     shipped after initial release).
   - `papers.favorite=FALSE` similarly.
5. Wiki identity reconciliation, in this order (order matters):
   - `backfill_wiki_identity(engine)` fills `entity_key` on rows that predate
     the column. The key derivation (`_derive_entity_key`) prefers the
     OpenAlex A-ID for scholars, falls back to `scholar:name:<normalized>`
     for `name--*` slugs, then to `scholar:slug:<title>`, and finally to
     `<kind>:slug:<slug>` for non-scholar pages.
   - `retire_duplicate_wiki_pages(engine)` converts duplicate content pages
     for the same `entity_key` into redirect shells (picking the canonical by
     A-ID match, then wiki_sources count as tiebreaker). Redirect shells are
     written *before* the unique index is created.
   - `_ensure_wiki_identity_index(engine)` creates the partial unique index
     `uq_wiki_pages_entity_key_live`
     (`WHERE redirects_to IS NULL AND entity_key IS NOT NULL`).
6. HNSW cosine indexes, both best-effort:
   - `ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)`
     — may fail on pgvector versions that cap `vector` at 2000 dims when the
     model emits 2048-dim vectors; a failure is logged and search falls back
     to a sequential scan.
   - `ix_wiki_pages_embedding_hnsw ON wiki_pages USING hnsw (embedding halfvec_cosine_ops)`
     — the wiki layer uses `halfvec(2048)` specifically so it can carry a
     real HNSW index on pgvector ≥ 0.7.

There is no Alembic migration directory yet. The project treats
`init_db` + `_ensure_columns` as sufficient for a single-user local app;
introduce Alembic before a release that needs to rewrite or delete columns
on an existing database.

## Session dependency

`get_session_dep()` is a FastAPI dependency that yields a
`Session(get_app_engine())` per request and closes it on exit. Tests override
it (`tests/conftest.py`) with a session bound to an in-memory SQLite engine,
so API tests can seed/inspect rows through the same session object the route
uses.

For background tasks and scheduler bodies, do not use the dependency —
open a new session explicitly:

```python
engine = get_app_engine()
with Session(engine) as session:
    run_sync(session, app_config, ...)
```

## SQLite test dialect

`carrel/models.py` defines the vector columns as
`Vector(2048).with_variant(JSON(), "sqlite")` and
`HALFVEC(2048).with_variant(JSON(), "sqlite")`, so the full ORM works
in-memory. Embeddings round-trip through SQLite as JSON lists; ranking
code in `api/search.py` and `api/chat.py` has explicit SQLite branches
(`_semantic_search_sqlite`, `_rank_sqlite`) that decode the JSON and compute
cosine in Python. `tests/conftest.py` sets
`DATABASE_URL=sqlite:///<tmpfile>` *before* importing `carrel.config` so the
app engine boots against SQLite.

The `session` fixture creates an in-memory engine with `StaticPool` and
`check_same_thread=False`, runs `SQLModel.metadata.create_all`, and yields a
Session. The `client` fixture reuses that engine via dependency override and
runs the real lifespan (which is idempotent on an already-created schema).

## Wiki identity: storage format and redirects

- A **live** page has `redirects_to IS NULL` and a unique `entity_key`.
- A **redirect shell** has `redirects_to = <canonical entity_key>`; it is
  allowed to share the canonical's `entity_key` (the partial unique index
  excludes it). On disk it is a small Markdown file whose frontmatter
  declares `redirects_to`.
- Resolution walks at most `_MAX_ALIAS_HOPS = 8` hops (paper) /
  `_MAX_REDIRECT_HOPS = 4` hops (wiki) to defend against cycles.
- The `resolve_target` cache in `pipeline/wiki/_links.py` is cleared by
  `reindex_wiki` so post-merge lookups do not return stale ids.

See [../wiki/reconciliation.md](../wiki/reconciliation.md) for the full
reconciliation algorithm.

## Focused tests

- `tests/conftest.py` — SQLite engine/session/client fixtures.
- `tests/test_wiki_reconcile.py` — `reconcile_kind`, redirects, cycles.
- `tests/test_wiki_reindex.py` — disk-to-index rebuild, backlinks.
- `tests/test_migrate_paper_dedup.py` — one-shot paper-alias migration uses
  the same engine/session primitives.

## Validation

```bash
.venv/bin/python -m pytest tests/test_wiki_reconcile.py tests/test_wiki_reindex.py tests/test_api.py -q
```

To inspect the live schema:

```bash
make psql
# \d papers
# \d wiki_pages
```

## Evidence

- Engine/bootstrap: `carrel/db.py`.
- Table definitions and vector column variants: `carrel/models.py`.
- Lifespan calls into `init_db`: `carrel/main.py:79-97`.
- SQLite fixtures: `tests/conftest.py`.
- Postgres service definition: `docker-compose.yml`.
