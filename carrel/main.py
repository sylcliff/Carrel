"""FastAPI application factory and lifespan."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

# Load .env into os.environ as early as possible. pydantic-settings reads
# .env on its own, but helpers like carrel.embeddings._key_for read os.environ
# directly — without this they miss keys that only exist in the file.
load_dotenv()

from carrel import __version__
from carrel.api import (
    agent_runs,
    annotations,
    authors_backfill,
    chat,
    citations,
    embed,
    health,
    import_bulk,
    mcp,
    papers,
    paper_dedup,
    paper_extract,
    process,
    publication,
    schedule,
    scholar_dedup,
    scholar_works_sync,
    scholars,
    search,
    search_brave,
    settings,
    subscriptions,
    summarize,
    sync,
    topics,
    usage,
    wiki,
    wiki_chat,
)
from carrel.config import CarrelYAML, EnvSettings, load_settings
from carrel.db import init_app_engine, init_db
from carrel.mcp import start_mcp, stop_mcp
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
    cfg.storage.wiki_dir().mkdir(parents=True, exist_ok=True)
    for kind in ("concept", "scholar", "question"):
        cfg.storage.wiki_kind_dir(kind).mkdir(parents=True, exist_ok=True)
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
    # MCP integration is optional and best-effort: a missing dependency
    # (no Node.js), a bad key, or a hung subprocess should never prevent
    # Carrel from starting. The /mcp/health endpoint reports the state.
    try:
        await start_mcp(cfg, env)
    except Exception:
        logger.exception("MCP startup failed; continuing without MCP")
    logger.info(
        "Carrel %s started. db=%s mineru=%s",
        __version__, env.database_url, cfg.mineru.base_url,
    )
    yield
    try:
        await stop_mcp()
    except Exception:
        logger.exception("error stopping MCP registry")
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

    # Belt-and-suspenders: any ``text/event-stream`` response is forced to
    # ``Cache-Control: no-store`` regardless of what the route set. SSE
    # streams (paper chat, wiki chat) must never be cached by the browser
    # or by a reverse proxy. The middleware wraps the response so it
    # fires after the route handler returns.
    @app.middleware("http")
    async def _no_store_sse(request: Request, call_next):
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        # ``StreamingResponse`` sets ``media_type`` after construction; the
        # resulting response header is the source of truth at this point.
        if content_type.startswith("text/event-stream") or isinstance(
            response, StreamingResponse
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(health.router)
    app.include_router(papers.router)
    app.include_router(citations.router)
    app.include_router(annotations.router)
    app.include_router(subscriptions.router)
    app.include_router(schedule.router)
    app.include_router(sync.router)
    app.include_router(process.router)
    app.include_router(publication.router)
    app.include_router(summarize.router)
    app.include_router(topics.router)
    app.include_router(scholars.router)
    app.include_router(wiki.router)
    app.include_router(wiki_chat.router)
    app.include_router(paper_extract.router)
    app.include_router(authors_backfill.router)
    app.include_router(scholar_works_sync.router)
    app.include_router(scholar_dedup.router)
    app.include_router(paper_dedup.router)
    app.include_router(embed.router)
    app.include_router(search.router)
    app.include_router(search_brave.router)
    app.include_router(import_bulk.router)
    app.include_router(mcp.router)
    app.include_router(chat.router)
    app.include_router(settings.router)
    app.include_router(usage.router)
    app.include_router(agent_runs.router)

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
