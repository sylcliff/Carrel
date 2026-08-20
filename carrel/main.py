"""FastAPI application factory and lifespan."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from carrel import __version__
from carrel.api import (
    annotations,
    citations,
    embed,
    health,
    papers,
    process,
    search,
    subscriptions,
    summarize,
    sync,
)
from carrel.config import CarrelYAML, EnvSettings, load_settings
from carrel.db import init_app_engine, init_db
from carrel.scheduler import start_scheduler, stop_scheduler
from carrel.sources import openalex_client as oa
from carrel.sources import semanticscholar_client as s2

logger = logging.getLogger("carrel")

CONFIG_PATH = Path("data/config.yaml")

# Populated in lifespan; routers import from here to keep state without DI gymnastics.
app_config: CarrelYAML
app_env: EnvSettings


def _bootstrap_config() -> tuple[CarrelYAML, EnvSettings]:
    """Load config once at import time so CORS can read it before the app starts.

    The lifespan reuses the same values; we do not reload so behavior is
    consistent across middleware, routers and background tasks.
    """
    cfg, env = load_settings(CONFIG_PATH)
    cfg.storage.root.mkdir(parents=True, exist_ok=True)
    cfg.storage.paper_dir().mkdir(parents=True, exist_ok=True)
    cfg.storage.attachments_dir().mkdir(parents=True, exist_ok=True)
    return cfg, env


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global app_config, app_env

    cfg, env = _bootstrap_config()

    engine = init_app_engine(env)
    # Always make sure pgvector exists; SQLModel.create_all will create the
    # tables on first run so the frontend can boot against a fresh DB.
    init_db(engine)

    # A restart (or --reload) can orphan jobs left in queued/running by a dead
    # worker. Mark them failed so the UI doesn't poll them forever.
    from datetime import UTC, datetime

    from sqlalchemy import text
    from sqlmodel import Session

    with Session(engine) as _s:
        _s.execute(text(
            "UPDATE jobs SET status='failed', finished_at=:now, "
            "message='Interrupted by server restart' "
            "WHERE status IN ('queued','running')"
        ), {"now": datetime.now(UTC)})
        _s.commit()

    # Shared HTTP clients for external metadata sources.
    s2.configure(
        base_url=cfg.semantic_scholar.base_url,
        api_key=cfg.semantic_scholar.api_key,
        timeout=cfg.semantic_scholar.request_timeout_seconds,
        max_retries=cfg.semantic_scholar.max_retries,
        rate_limit_per_second=cfg.semantic_scholar.rate_limit_per_second,
    )
    # pyalex is configured lazily by the sync pipeline; configure it here too
    # so /search gets the polite-pool mailto and the connect/read timeout.
    oa.configure(cfg)

    app_config = cfg
    app_env = env
    start_scheduler(cfg)
    logger.info(
        "Carrel %s started. db=%s mineru=%s",
        __version__, env.database_url, cfg.mineru.base_url,
    )
    yield
    stop_scheduler()


def create_app() -> FastAPI:
    # Load config at build time so CORS reflects configured origins rather
    # than a permissive "*". A localhost/Vite default is always allowed so the
    # dev server works even with no config.yaml present.
    try:
        cfg, _env = _bootstrap_config()
        cors_origins = list(dict.fromkeys([
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            *cfg.cors.origins,
        ]))
    except Exception:  # pragma: no cover - defensive; lifespan will surface real errors
        logger.exception("failed to load config at startup; using default CORS origins")
        cors_origins = ["http://127.0.0.1:5173", "http://localhost:5173"]

    app = FastAPI(
        title="Carrel",
        version=__version__,
        description="Self-hosted single-user paper reading room",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(papers.router)
    app.include_router(citations.router)
    app.include_router(annotations.router)
    app.include_router(subscriptions.router)
    app.include_router(sync.router)
    app.include_router(process.router)
    app.include_router(summarize.router)
    app.include_router(embed.router)
    app.include_router(search.router)

    # Serve parsed markdown images (and PDFs) straight from storage. The
    # bootstrap step above created the directory, so StaticFiles can mount it.
    # Mounted last so it doesn't shadow API routes.
    try:
        storage_root = cfg.storage.root.resolve()
        app.mount(
            "/storage",
            StaticFiles(directory=str(storage_root)),
            name="storage",
        )
    except Exception:  # pragma: no cover - defensive; directory was just created
        logger.exception("could not mount /storage static directory")

    return app


app = create_app()


# Session dependency lives in carrel.db.get_session_dep
from carrel.db import get_app_engine, get_session_dep  # noqa: E402, F401
