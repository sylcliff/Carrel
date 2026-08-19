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
    })

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
