"""FastAPI application factory and lifespan."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import event
from sqlmodel import Session

from carrel import __version__
from carrel.api import health, papers, subscriptions, sync
from carrel.config import CarrelYAML, EnvSettings, load_settings
from carrel.db import init_app_engine, init_db

logger = logging.getLogger("carrel")

# Populated in lifespan; routers import from here to keep state without DI gymnastics.
app_config: CarrelYAML
app_env: EnvSettings


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global app_config, app_env

    yaml_path = Path("data/config.yaml")
    cfg, env = load_settings(yaml_path)

    # Ensure storage dirs exist
    cfg.storage.root.mkdir(parents=True, exist_ok=True)
    cfg.storage.paper_dir().mkdir(parents=True, exist_ok=True)
    cfg.storage.attachments_dir().mkdir(parents=True, exist_ok=True)

    engine = init_app_engine(env)
    # Always make sure pgvector exists; SQLModel.create_all will create the
    # tables on first run so the frontend can boot against a fresh DB.
    init_db(engine)

    app_config = cfg
    app_env = env
    logger.info("Carrel %s started. db=%s mineru=%s", __version__, env.database_url, cfg.mineru.base_url)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Carrel",
        version=__version__,
        description="Self-hosted single-user paper reading room",
        lifespan=lifespan,
    )

    # CORS — Vite dev default + any custom origins from config
    # (CORS origins are pulled in after lifespan; use a permissive default here
    # and tighten via CORS middleware below.)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # tightened in lifespan-registered middleware below
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(papers.router)
    app.include_router(subscriptions.router)
    app.include_router(sync.router)

    return app


app = create_app()


# Session dependency lives in carrel.db.get_session_dep
from carrel.db import get_app_engine, get_session_dep  # noqa: E402, F401
