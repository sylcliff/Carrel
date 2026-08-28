"""Liveness / readiness endpoint. Used by the frontend boot probe.

Pass ``?debug=1`` to also receive :func:`AppCache.stats` so operators can
inspect the in-process L2 cache without instrumenting the running app.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlmodel import Session

from carrel import __version__
from carrel.db import get_session_dep
from carrel.schemas import HealthResponse
from carrel.sources import remote_downloader

router = APIRouter(tags=["health"])


def _get_app_config():
    # Late import to avoid circular: main -> api -> main
    from carrel.main import app_config

    return app_config


@router.get("/health", response_model=HealthResponse)
def health(
    session: Session = Depends(get_session_dep),
    debug: int = Query(0, description="Include L2 AppCache stats when 1"),
) -> HealthResponse:
    db_status = "down"
    try:
        result = session.exec(text("SELECT 1")).first()
        if result and result[0] == 1:
            db_status = "up"
    except Exception:  # pragma: no cover - readiness probe
        db_status = "down"

    cfg = _get_app_config()
    cache_stats = None
    if debug:
        # Imported lazily so the default boot probe has zero overhead
        # from the L2 machinery if the import path is cold.
        from carrel.api._app_cache import get_cache

        cache_stats = get_cache().stats()
    return HealthResponse(
        status="ok",
        version=__version__,
        db=db_status,
        mineru=cfg.mineru.base_url,
        remote=remote_downloader.is_configured(),
        cache=cache_stats,
    )
