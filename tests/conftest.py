"""Pytest fixtures.

Unit tests use an in-memory SQLite session directly. API smoke tests boot the
real FastAPI app (lifespan, routers, dependencies) but point its engine at an
in-memory SQLite database, so no Docker/Postgres is required.

SQLModel's Vector column has a String variant for SQLite
(see carrel.models.VectorType), so the full ORM works in-memory.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest

# Make the FastAPI lifespan boot against SQLite (not Postgres) so the API
# smoke tests run without Docker. Must be set BEFORE carrel.config is imported.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="-carrel-test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from carrel import models  # noqa: F401,E402  (register tables on metadata)
from carrel.config import CarrelYAML  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def cfg() -> CarrelYAML:
    return CarrelYAML()


import atexit  # noqa: E402

atexit.register(lambda: (os.close(_DB_FD), os.unlink(_DB_PATH)))


@pytest.fixture()
def client(session: Session):
    """FastAPI TestClient backed by the same in-memory engine as `session`.

    We override the get_session_dep so API calls use the in-memory database;
    tests can seed/inspect rows via the `session` fixture.
    """
    from carrel.db import get_session_dep
    from carrel.main import app
    from fastapi.testclient import TestClient

    # The engine bound to the in-memory connection used by `session`.
    engine = session.get_bind()

    def _override_session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session_dep] = _override_session
    # lifespan creates tables (idempotent) and storage dirs.
    # The L2 cache (AppCache) is a process-wide singleton; reset it
    # between tests so the @cached helpers don't return stale results
    # when a test mutates a row directly (bypassing the invalidation
    # hooks). Phase 3 added this fixture.
    from carrel.api._app_cache import reset_cache_for_tests

    reset_cache_for_tests()
    with TestClient(app) as c:
        yield c
    reset_cache_for_tests()
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_throttle_singleton():
    """Reset the OpenAlex throttle singleton between tests so a recorded
    latch in test_throttle.py doesn't bleed into other tests' pipeline
    code (which has top-of-loop ``openalex_throttle.is_open()`` checks that
    would short-circuit if the singleton were open).
    """
    from carrel.sources.throttle import openalex_throttle

    openalex_throttle.clear()
    yield
    openalex_throttle.clear()
