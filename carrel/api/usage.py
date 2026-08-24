"""Token usage read-only endpoints (M13).

Five GETs:
  * ``/usage/summary``     — totals (prompt/completion/total/calls)
  * ``/usage/by-model``    — grouped by ``model``
  * ``/usage/by-feature``  — grouped by ``feature``
  * ``/usage/by-day``      — per-day series for the last ``days`` (default 30)
  * ``/usage/recent``      — most recent N rows

All accept an optional ``since_days`` query param (``summary``, ``by_model``,
``by_feature``) so the UI can show 7d / 30d / all-time windows without
hitting the DB twice.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from carrel import usage
from carrel.db import get_session_dep

router = APIRouter(tags=["usage"])


@router.get("/usage/summary")
def get_summary(
    since_days: int | None = Query(default=None, ge=1, le=3650),
    session: Session = Depends(get_session_dep),
) -> dict:
    return usage.summary(session, since_days=since_days)


@router.get("/usage/by-model")
def get_by_model(
    since_days: int | None = Query(default=None, ge=1, le=3650),
    session: Session = Depends(get_session_dep),
) -> list[dict]:
    return usage.by_model(session, since_days=since_days)


@router.get("/usage/by-feature")
def get_by_feature(
    since_days: int | None = Query(default=None, ge=1, le=3650),
    session: Session = Depends(get_session_dep),
) -> list[dict]:
    return usage.by_feature(session, since_days=since_days)


@router.get("/usage/by-day")
def get_by_day(
    days: int = Query(default=30, ge=1, le=365),
    session: Session = Depends(get_session_dep),
) -> list[dict]:
    return usage.by_day(session, days=days)


@router.get("/usage/recent")
def get_recent(
    limit: int = Query(default=20, ge=1, le=200),
    session: Session = Depends(get_session_dep),
) -> list[dict]:
    return usage.recent(session, limit=limit)
