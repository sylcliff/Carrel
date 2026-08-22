"""Liveness / readiness endpoint. Used by the frontend boot probe."""
from __future__ import annotations

from fastapi import APIRouter, Depends
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
def health(session: Session = Depends(get_session_dep)) -> HealthResponse:
    db_status = "down"
    try:
        result = session.exec(text("SELECT 1")).first()
        if result and result[0] == 1:
            db_status = "up"
    except Exception:  # pragma: no cover - readiness probe
        db_status = "down"

    cfg = _get_app_config()
    return HealthResponse(
        status="ok",
        version=__version__,
        db=db_status,
        mineru=cfg.mineru.base_url,
        remote=remote_downloader.is_configured(),
    )
