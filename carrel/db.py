"""Database engine, session, and bootstrap utilities."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from carrel.config import EnvSettings


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
    """Create all tables and the pgvector extension.

    M1 only needs this for ad-hoc debugging. M2 will switch to Alembic.
    The pgvector extension is only requested on PostgreSQL; SQLite (used in
    tests/smoke) will silently skip it.
    """
    # Import models so SQLModel.metadata sees them before create_all.
    from carrel import models  # noqa: F401

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector;")
    SQLModel.metadata.create_all(engine)


def get_session_factory(engine: Engine) -> type[Session]:
    # SQLModel's Session is a thin wrapper; we just bind a default.
    return Session


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
