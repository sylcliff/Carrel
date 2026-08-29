"""Database engine, session, and bootstrap utilities."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine

from carrel.config import EnvSettings

logger = logging.getLogger(__name__)


def make_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy engine with sensible defaults for a single-user app."""
    engine = create_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
        # For a single-user local app, default pool is fine; we don't share
        # connections across processes.
    )

    # Ensure pgvector works with our connection string: register the vector type
    # on every new connection. Only relevant for PostgreSQL — SQLite has no
    # concept of extensions and its cursor does not support `with`.
    if engine.dialect.name == "postgresql":

        @event.listens_for(engine, "connect")
        def _register_vector(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            with dbapi_conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    return engine


def init_db(engine: Engine) -> None:
    """Create all tables and the pgvector extension (startup bootstrap).

    This is intentionally simple — a single-user local app on a fresh database
    can create its schema directly. Alembic migrations can be introduced before
    a release that needs to upgrade an existing database.
    The pgvector extension is only requested on PostgreSQL; SQLite (used in
    tests) silently skips it.
    """
    # Import models so SQLModel.metadata sees them before create_all.
    from carrel import models  # noqa: F401

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector;")
    SQLModel.metadata.create_all(engine)

    # Lightweight, additive migrations for columns added after the initial
    # release. The project has no Alembic setup yet (single-user local app), so
    # `create_all` above only creates missing *tables* — never new columns on
    # an existing table. Each statement is idempotent.
    _ensure_columns(engine, "papers", {
        "references": "JSON",
        # Inbox: sync discovers candidates (in_library=False); the user imports
        # a paper to flip it. Existing rows predate the feature and are all in
        # the library, so the column is added with a DEFAULT and backfilled.
        # Use TRUE/FALSE literals (Postgres rejects integer 1 as a boolean
        # default; SQLite accepts both).
        "in_library": "BOOLEAN DEFAULT TRUE NOT NULL",
        "discarded": "BOOLEAN DEFAULT FALSE NOT NULL",
        # Postgres has no DATETIME type; TIMESTAMP works on both it and SQLite.
        "discovered_at": "TIMESTAMP",
        # User annotations (M7).
        "favorite": "BOOLEAN DEFAULT FALSE NOT NULL",
        "notes_markdown": "TEXT",
        # Institutional SSH download + arXiv→journal detection.
        "pdf_origin": "VARCHAR(16)",
        "journal_doi": "VARCHAR(255)",
        "pdf_files": "JSON",
        "published_checked_at": "TIMESTAMP",
        # Structured paper card (LLM extraction, optional).
        "paper_card": "JSON",
        "paper_card_extracted_at": "TIMESTAMP",
    })
    _ensure_columns(engine, "wiki_pages", {
        # Wiki identity decoupling (M-reconcile): see carrel/pipeline/wiki/_entities.py
        "entity_key": "VARCHAR(200)",
        "redirects_to": "VARCHAR(200)",
        # Stub pages: below-threshold concepts/questions written without an
        # LLM call. The wiki list view filters stubs out by default.
        "stub": "BOOLEAN DEFAULT FALSE NOT NULL",
    })
    _ensure_columns(engine, "paper_concepts", {
        # Concept category from the extraction LLM (METHOD/THEORY/DATASET/
        # DOMAIN/PHENOMENON).  Nullable: NULL means "uncategorized" (e.g.
        # rows extracted before this column existed).
        "category": "VARCHAR(32)",
    })

    # Backfill: papers created before the inbox feature existed are already in
    # the library. Only touches rows whose column came back NULL (SQLite treats
    # a missing-at-ADD COLUMN DEFAULT inconsistently across versions).
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE papers SET in_library=TRUE WHERE in_library IS NULL"
            )
    else:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE papers SET in_library=1 WHERE in_library IS NULL"
            )

    # Backfill favorite for rows added before the column existed.
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE papers SET favorite=FALSE WHERE favorite IS NULL"
            )
    else:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE papers SET favorite=0 WHERE favorite IS NULL"
            )

    # Wiki identity reconciliation.  The order matters: backfill entity_key
    # for existing rows, then retire duplicates (writing redirect shells
    # *before* creating the partial unique index — otherwise the index
    # creation would fail on the duplicates it was meant to prevent).
    backfill_wiki_identity(engine)
    retire_duplicate_wiki_pages(engine)
    _ensure_wiki_identity_index(engine)

    # OpenAlex persistent cache (A+B). Indexes use raw DDL because
    # SQLAlchemy's `Index(..., postgresql_where=...)` silently drops the
    # WHERE clause on SQLite, which would turn the partial indexes into
    # full ones. ``DESC`` is honored on both PG and SQLite ≥ 3.3.
    _ensure_openalex_cache_indexes(engine)
    # Crash recovery: any author_works_sync row left in `loading` by a
    # killed server would otherwise pin the scholar page forever; mirror
    # the orphan-Job cleanup that main.lifespan runs for the jobs table.
    _reset_orphaned_openalex_sync(engine)

    # HNSW index for cosine similarity over chunk embeddings. Built once at
    # startup (IF NOT EXISTS makes it idempotent). HNSW defaults (m=16,
    # ef_construction=64) are fine for <100k chunks; revisit if recall drops
    # or build time grows.
    #
    # Best-effort: pgvector's HNSW caps `vector` at 2000 dims, but some
    # embedding models (e.g. doubao-embedding-large, 2048) exceed that. An
    # index build failure must not block startup — a local single-user library
    # can tolerate a sequential scan over chunks until the column is migrated
    # to `halfvec` (which supports up to 4000 dims) or pgvector is upgraded.
    if engine.dialect.name == "postgresql":
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw "
                    "ON chunks USING hnsw (embedding vector_cosine_ops)"
                )
        except OperationalError as exc:  # e.g. >2000 dims on pgvector < 0.8
            logger.warning(
                "Could not create HNSW index on chunks.embedding; semantic "
                "search will fall back to a sequential scan. Cause: %s",
                exc.orig,
            )

        # Wiki/memory embeddings use halfvec(2048), which supports HNSW (the
        # halfvec opclass accepts up to 4000 dims). Requires pgvector server
        # extension >= 0.7.0; older versions fail here and fall back to a
        # sequential scan without blocking startup.
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_wiki_pages_embedding_hnsw "
                    "ON wiki_pages USING hnsw (embedding halfvec_cosine_ops)"
                )
        except OperationalError as exc:
            logger.warning(
                "Could not create HNSW index on wiki_pages.embedding; wiki "
                "search will fall back to a sequential scan. Cause: %s",
                exc.orig,
            )


def get_session_factory(engine: Engine) -> type[Session]:
    # SQLModel's Session is a thin wrapper; we just bind a default.
    return Session


def _ensure_columns(engine: Engine, table: str, columns: dict[str, str]) -> None:
    """Add missing columns to an existing table (idempotent, additive only).

    `create_all` never alters an existing table, so columns introduced after a
    user first ran the app must be added explicitly. Values are the dialect's
    column type declaration. SQLite and PostgreSQL are both handled.
    """
    from sqlalchemy import column, inspect as sa_inspect, table as sa_table

    inspector = sa_inspect(engine)
    existing = {col["name"] for col in inspector.get_columns(table)}
    # Quote identifiers — some column names (e.g. `references`) are SQL reserved
    # words. SQLAlchemy's preparer handles dialect-specific quoting.
    preparer = engine.dialect.identifier_preparer
    qtable = preparer.format_table(sa_table(table))
    with engine.begin() as conn:
        for name, col_type in columns.items():
            if name in existing:
                continue
            qcol = preparer.format_column(column(name))
            conn.exec_driver_sql(
                f"ALTER TABLE {qtable} ADD COLUMN {qcol} {col_type}"
            )


# ---------------------------------------------------------------------------
# Wiki identity (entity_key, redirects_to) — see carrel/pipeline/wiki/_entities.py
# ---------------------------------------------------------------------------

# Partial unique index DDL.  The same statement works on both Postgres and
# SQLite (3.8+ in `CREATE INDEX` form); the `WHERE` predicate is honored on
# both engines.  We can't use SQLAlchemy's `Index(unique=True, postgresql_where=...)`
# because the `where=` clause is silently dropped on SQLite, which would turn
# the index into a global unique constraint and break redirect shells (they
# share an entity_key with their canonical on purpose).
_WIKI_ENTITY_KEY_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_wiki_pages_entity_key_live "
    "ON wiki_pages (entity_key) "
    "WHERE redirects_to IS NULL AND entity_key IS NOT NULL"
)


def _ensure_wiki_identity_index(engine: Engine) -> None:
    """Create the partial unique index over live (non-redirect) wiki pages.

    Safe to call repeatedly: `IF NOT EXISTS` short-circuits when the index
    already exists.  Catches OperationalError (Postgres) and sqlite's
    IntegrityError-equivalent on rare lock-contention scenarios so a
    transient failure does not brick startup.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql(_WIKI_ENTITY_KEY_INDEX_DDL)


# ---------------------------------------------------------------------------
# OpenAlex persistent cache (A+B) — see carrel/models.py for the tables.
# ---------------------------------------------------------------------------

# Composite (author_id, publication_date DESC, cited_by_count DESC) drives
# the scholar page's "newest first" sort without a filesort, and the two
# partial indexes keep DOI/arXiv lookups O(log n) even as the cache grows.
_OPENALEX_CACHE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_awc_author_date "
    "ON author_works_cache (author_id, publication_date DESC, cited_by_count DESC)",
    "CREATE INDEX IF NOT EXISTS ix_awc_doi "
    "ON author_works_cache (doi) WHERE doi IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_awc_arxiv "
    "ON author_works_cache (arxiv_id) WHERE arxiv_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_wbaid_openalex "
    "ON work_by_arxiv_id (openalex_id)",
)


def _ensure_openalex_cache_indexes(engine: Engine) -> None:
    """Create the indexes backing :class:`AuthorWorksCache` /
    :class:`WorkByArxivId`. Idempotent via ``IF NOT EXISTS``.

    Mirrors the partial-index pattern used for wiki identity — `Index(...,
    postgresql_where=...)` is silently dropped on SQLite, so raw DDL is
    the only way to keep partial WHERE clauses portable.
    """
    with engine.begin() as conn:
        for ddl in _OPENALEX_CACHE_INDEXES:
            conn.exec_driver_sql(ddl)


def _reset_orphaned_openalex_sync(engine: Engine) -> None:
    """Mark every ``author_works_sync.status='loading'`` row as failed.

    Runs at startup so a crashed/killed server doesn't pin its in-flight
    scholars in a forever-loading state (the in-process worker that owned
    them is gone, and there's no other actor that would re-mark them).
    Mirrors the orphan-Job cleanup at :mod:`carrel.main` (lifespan).
    """
    from datetime import UTC, datetime

    from sqlmodel import Session as SqlSession
    from sqlalchemy import update

    from carrel.models import AuthorWorksSync

    with SqlSession(engine) as session:
        session.exec(
            update(AuthorWorksSync)
            .where(AuthorWorksSync.status == "loading")
            .values(
                status="failed",
                last_error="Interrupted by server restart",
                updated_at=datetime.now(UTC),
            )
        )
        session.commit()


def backfill_wiki_identity(engine: Engine) -> dict[str, int]:
    """Populate ``entity_key`` for existing ``wiki_pages`` rows.

    Idempotent — rows whose ``entity_key`` is already set are left alone.
    Returns counts of rows touched.  Called by :func:`init_db` and by the
    standalone cleanup script; safe to run on a fresh database.
    """
    from datetime import UTC, datetime
    from sqlmodel import Session, select

    from carrel.models import WikiPage

    counts = {"updated": 0, "skipped": 0}
    with Session(engine) as session:
        rows = session.exec(
            select(WikiPage).where(WikiPage.entity_key.is_(None))
        ).all()
        if not rows:
            return counts
        for row in rows:
            key = _derive_entity_key(row)
            if key is None:
                counts["skipped"] += 1
                continue
            row.entity_key = key
            session.add(row)
            counts["updated"] += 1
        session.commit()
    return counts


def _derive_entity_key(row) -> str | None:
    """Compute the canonical ``entity_key`` for a wiki page row."""
    # Imported lazily to avoid a circular import at module load — db.py is
    # imported by the very modules _scholars_agg / _names depend on.
    from carrel.pipeline.wiki._names import normalize_name

    if row.kind == "scholar":
        if row.scholar_aid:
            return f"scholar:{row.scholar_aid}"
        if row.slug and row.slug.startswith("name--"):
            # The slug form is "name--<normalized-name>"; turn it back into
            # the same key the aggregator produces.  ``title`` may be
            # the original display spelling.
            base = row.slug[len("name--"):]
            if base:
                return f"scholar:name:{base}"
        # Fallback: a non-AID scholar page whose slug doesn't follow the
        # name-- convention.  We synthesize from the title so a future
        # reconcile can repair it once the slug is fixed.
        if row.title:
            return f"scholar:slug:{normalize_name(row.title)}"
    # Future kinds: a placeholder keyed by slug.  When the concept/question
    # enumerators are implemented, a subsequent reconcile pass will replace
    # these with their real entity_key.
    if row.slug:
        return f"{row.kind}:slug:{row.slug}"
    return None


def retire_duplicate_wiki_pages(engine: Engine) -> dict[str, int]:
    """Convert duplicate ``entity_key`` content pages into redirect shells.

    For each ``entity_key`` with more than one non-redirect row, pick the
    canonical (the row whose ``scholar_aid`` matches the current aggregated
    key for that entity, or the one with the most ``wiki_sources`` as a
    tiebreaker) and convert the rest:

      * DB: ``entity_key = NULL``, ``redirects_to = canonical.entity_key``,
        ``title = canonical.title``, ``summary = NULL``,
        ``confidence = 0``, ``evidence_count = 0``, ``compiled_at = now()``.
      * On disk: rewrite the loser's ``.md`` file as a redirect shell
        (frontmatter ``redirects_to:`` + one-line body stub).  Files that
        do not exist (never-compiled rows) are left alone.
      * ``wiki_sources``: re-point every loser's rows to the canonical id
        so the canonical's "Sources" footer still cites them.

    Idempotent — re-running on a clean DB is a no-op.  Returns counters.
    """
    from datetime import UTC, datetime
    from sqlmodel import Session, select

    from carrel.models import WikiPage, WikiSource
    from carrel.pipeline.wiki._frontmatter import dump

    counts = {"retired": 0, "skipped": 0, "moved_sources": 0}
    with Session(engine) as session:
        # Group by entity_key, filter to non-redirect rows with > 1 entry.
        all_live = session.exec(
            select(WikiPage).where(WikiPage.redirects_to.is_(None))
        ).all()
        groups: dict[str, list[WikiPage]] = {}
        for row in all_live:
            if not row.entity_key:
                continue
            groups.setdefault(row.entity_key, []).append(row)
        now = datetime.now(UTC)
        for key, rows in groups.items():
            if len(rows) <= 1:
                counts["skipped"] += 1
                continue
            canonical = _pick_canonical(rows)
            for row in rows:
                if row.id == canonical.id:
                    continue
                # DB row → redirect shell
                row.entity_key = None
                row.redirects_to = canonical.entity_key
                row.title = canonical.title
                row.summary = None
                row.confidence = 0.0
                row.evidence_count = 0
                row.compiled_at = now
                session.add(row)
                # Move WikiSource rows to the canonical id
                sources = session.exec(
                    select(WikiSource).where(WikiSource.wiki_page_id == row.id)
                ).all()
                for s in sources:
                    s.wiki_page_id = canonical.id
                    session.add(s)
                counts["moved_sources"] += len(sources)
                # Rewrite the file on disk (if it exists). The page's ``path``
                # field is storage-root-relative. Try the configured storage
                # root first (the dev app's normal layout), then fall back to
                # CWD so the standalone cleanup script works regardless of
                # where it was invoked from.
                try:
                    from pathlib import Path

                    full = _resolve_storage_path(row.path)
                    if full.exists():
                        meta = {"redirects_to": canonical.entity_key}
                        body = (
                            f"# Redirected\n\n"
                            f"This page moved to "
                            f"[[{canonical.title}]]({_rel_link(row, canonical)}).\n"
                        )
                        text = dump(meta, body)
                        tmp = full.with_suffix(full.suffix + ".tmp")
                        tmp.write_text(text, encoding="utf-8")
                        tmp.replace(full)
                except OSError:
                    pass
                counts["retired"] += 1
        session.commit()
    # Recreate the partial unique index — callers that drop it (e.g. a
    # one-off migration in psql) get the guarantee back automatically.
    _ensure_wiki_identity_index(engine)
    return counts


def _rel_link(row, canonical) -> str:
    """Best-effort relative path from a redirect shell to its canonical."""
    from urllib.parse import quote
    return f"../{row.kind}s/{quote(canonical.slug)}.md"


def _resolve_storage_path(rel: str) -> Path:
    """Resolve a storage-root-relative path to an absolute filesystem path.

    Looks at the app's YAML config (when available) for ``storage.root``;
    falls back to the current working directory so the standalone cleanup
    script works whether or not the app engine is initialized.
    """
    from pathlib import Path as _Path

    p = _Path(rel)
    if p.is_absolute():
        return p
    # Prefer the app's configured storage root; fall back to CWD.
    root: _Path | None = None
    try:
        from carrel.config import CarrelYAML
        root = CarrelYAML().storage.root
    except Exception:
        root = None
    base = root if root else _Path.cwd()
    return base / p


def _pick_canonical(rows) -> WikiPage:
    """Pick the row that should remain a content page.

    Preference order:
      1. The row whose ``scholar_aid`` (if set) is currently aggregated as a
         live author key — verified indirectly by preferring rows that are
         *not* ``name--`` slugs over rows that are.
      2. The row with the most ``wiki_sources`` rows (most provenance).
      3. The oldest row (stable tiebreak).
    """
    # Preference: A-ID rows beat name-- rows; among ties, evidence wins.
    def _score(r):
        is_aid = bool(r.scholar_aid and r.scholar_aid.startswith("A"))
        return (1 if is_aid else 0, r.evidence_count, -(r.id or 0))
    return max(rows, key=_score)


def session_dep(
    engine: Annotated[Engine, Depends(lambda: get_app_engine())]
) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


# Module-level singleton. Initialized in main.lifespan.
app_engine: Engine | None = None


def get_app_engine() -> Engine:
    if app_engine is None:
        raise RuntimeError("DB engine not initialized — call init_app_engine() first")
    return app_engine


def init_app_engine(env: EnvSettings) -> Engine:
    global app_engine
    if app_engine is None:
        app_engine = make_engine(env.database_url)
    return app_engine


def get_session_dep() -> Iterator[Session]:  # type: ignore[no-untyped-def]
    """FastAPI dependency: yield a Session bound to the app engine.

    Implemented here (not via Depends) so routers can `from carrel.db import
    get_session_dep` without triggering the main.lifespan import cycle.
    """
    engine = get_app_engine()
    with Session(engine) as session:
        yield session
