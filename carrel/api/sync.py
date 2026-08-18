"""Sync trigger + job status endpoints (M2).

A POST /sync runs the fetch → normalize → dedup → upsert pipeline, either
inline (default, returns final stats) or as a BackgroundTask. Each run is
recorded as a Job and listed via GET /sync/jobs.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select

from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus
from carrel.pipeline.runner import run_sync
from carrel.schemas import JobOut, SyncRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("", response_model=JobOut)
def trigger_sync(
    body: SyncRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> JobOut:
    """Queue a sync job. M2 actually runs the fetch+upsert pipeline.

    `background=true` (default) runs the work in a BackgroundTask so the
    HTTP call returns immediately with a queued job. Pass `background=false`
    to run synchronously and get the final stats back in the job.
    """
    # We always create a job record first; the actual work either runs in
    # the background (returns queued->running->done) or inline (returns
    # done with full stats). For M2, inline is fine and gives the user
    # immediate feedback. We keep the BackgroundTasks plumbing ready for
    # later when downloads/MinerU/embedding are added.
    job = Job(
        kind=JobKind.sync.value,
        status=JobStatus.queued.value,
        message=f"queued (lookback_hours={body.lookback_hours})",
        stats={"lookback_hours": body.lookback_hours, "sources": body.sources},
        created_at=datetime.now(UTC),
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    if body.background:
        background.add_task(_run_in_background, job.id, body.lookback_hours)
    else:
        _run_inline(session, job.id, body.lookback_hours)

    # re-read job for fresh state
    session.refresh(job)
    return _to_out(job)


def _run_inline(session: Session, job_id: int, lookback_hours: int) -> None:
    from carrel.main import app_config

    try:
        run_sync(session, app_config, lookback_hours=lookback_hours, job=session.get(Job, job_id))
    except Exception as e:
        logger.exception("inline sync failed: %s", e)


def _run_in_background(job_id: int, lookback_hours: int) -> None:
    """Run the sync in a fresh DB session (BackgroundTask runs after response)."""
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine
    from carrel.main import app_config

    engine = get_app_engine()
    with SqlSession(engine) as session:
        try:
            run_sync(
                session,
                app_config,
                lookback_hours=lookback_hours,
                job=session.get(Job, job_id),
            )
        except Exception as e:
            logger.exception("background sync failed: %s", e)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    session: Session = Depends(get_session_dep),
    limit: int = 200,
    kind: str | None = None,
    status: str | None = None,
) -> list[JobOut]:
    stmt = select(Job).order_by(Job.id.desc()).limit(limit)
    if kind:
        stmt = stmt.where(Job.kind == kind)
    if status:
        stmt = stmt.where(Job.status == status)
    rows = session.exec(stmt).all()
    return [_to_out(r) for r in rows]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, session: Session = Depends(get_session_dep)) -> JobOut:
    row = session.get(Job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _to_out(row)


def _to_out(r: Job) -> JobOut:
    return JobOut(
        id=r.id or 0,
        kind=r.kind,
        status=r.status,
        message=r.message,
        stats=r.stats,
        started_at=r.started_at,
        finished_at=r.finished_at,
        created_at=r.created_at,
    )
