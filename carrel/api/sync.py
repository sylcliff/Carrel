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

from carrel.agent_recorder import (
    AgentRecorder,
    clear_current_recorder,
    pipeline_display_name,
    set_current_recorder,
)
from carrel.db import get_session_dep
from carrel.api._job_io import job_to_out
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
    return job_to_out(job)


def _run_inline(session: Session, job_id: int, lookback_hours: int) -> None:
    from carrel.main import app_config

    # Agent run + steps are recorded alongside the coarse Job. The
    # recorder is bound as the ambient one so the inner pipeline
    # functions can call ``agent_step(...)`` without threading the
    # recorder through every signature.
    rec = AgentRecorder(
        session, pipeline_id="sync", pipeline_name=pipeline_display_name("sync"),
        trigger="manual",
    )
    rec.start(
        context={"lookback_hours": lookback_hours, "sources": None},
        job_id=job_id,
    )
    token = set_current_recorder(rec)
    try:
        run_sync(session, app_config, lookback_hours=lookback_hours, job=session.get(Job, job_id))
        rec.finish(summary={"lookback_hours": lookback_hours})
    except Exception as e:
        rec.finish(status="failed", error=f"{type(e).__name__}: {e}")
        logger.exception("inline sync failed: %s", e)
    finally:
        clear_current_recorder(token)


def _run_in_background(job_id: int, lookback_hours: int) -> None:
    """Run the sync in a fresh DB session (BackgroundTask runs after response)."""
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine
    from carrel.main import app_config

    engine = get_app_engine()
    with SqlSession(engine) as session:
        # Separate recorder for the background path so the inline + bg
        # runs don't share a seq counter.
        rec = AgentRecorder(
            session, pipeline_id="sync", pipeline_name=pipeline_display_name("sync"),
            trigger="background",
        )
        rec.start(
            context={"lookback_hours": lookback_hours, "sources": None},
            job_id=job_id,
        )
        token = set_current_recorder(rec)
        try:
            run_sync(
                session,
                app_config,
                lookback_hours=lookback_hours,
                job=session.get(Job, job_id),
            )
            rec.finish(summary={"lookback_hours": lookback_hours})
        except Exception as e:
            rec.finish(status="failed", error=f"{type(e).__name__}: {e}")
            logger.exception("background sync failed: %s", e)
        finally:
            clear_current_recorder(token)


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
    return [job_to_out(r) for r in rows]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, session: Session = Depends(get_session_dep)) -> JobOut:
    row = session.get(Job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job_to_out(row)
