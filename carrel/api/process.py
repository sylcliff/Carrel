"""Processing endpoints (M3): download OA PDF + parse with MinerU.

``POST /process`` runs one paper (``paper_id``) or a batch of pending/failed
papers through ``pending -> pdf_ready -> parsed``. Each run is recorded as a
Job (kind ``download`` for single-paper, ``parse`` for batch — both cover the
full download+parse flow). Like sync, it can run inline (returns final stats)
or as a fire-and-forget BackgroundTask; in background mode the job's
``message``/``stats`` are updated live as each stage progresses, so the
frontend can poll and show progress.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session

from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus, Paper
from carrel.pipeline.process import ProcessError, process_paper, process_pending
from carrel.schemas import JobOut, ProcessRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/process", tags=["process"])


@router.post("", response_model=JobOut)
def trigger_process(
    body: ProcessRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> JobOut:
    if body.paper_id:
        paper = session.get(Paper, body.paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="paper not found")
        kind = JobKind.download.value
        message = f"process {body.paper_id}"
        stats = {"paper_id": body.paper_id, "stage": "queued", "detail": "Queued…"}
    else:
        kind = JobKind.parse.value
        message = f"process batch (limit={body.limit})"
        stats = {"limit": body.limit, "stage": "queued", "detail": "Queued…"}

    job = Job(
        kind=kind,
        status=JobStatus.queued.value,
        message=message,
        stats=stats,
        created_at=datetime.now(UTC),
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    if body.background:
        background.add_task(_run_background, job.id, body.paper_id, body.limit)
    else:
        _run_inline(session, job.id, body.paper_id, body.limit)

    session.refresh(job)
    return _to_out(job)


def _make_progress_cb(session: Session, job_id: int):
    """Return an on_progress callback that persists stage text to the Job."""

    def _cb(progress: dict) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        stage = progress.get("stage", "parse")
        detail = progress.get("detail", "")
        index = progress.get("index")
        total = progress.get("total")
        stats = {**(job.stats or {})}
        stats["stage"] = stage
        stats["detail"] = detail
        if "mineru_status" in progress:
            stats["mineru_status"] = progress["mineru_status"]
        if index is not None:
            stats["index"] = index
            stats["total"] = total
            title = progress.get("title")
            if title:
                stats["title"] = title[:120]
            prefix = f"[{index}/{total}] {title or ''}".rstrip()
            job.message = f"{prefix} — {detail}" if detail else prefix
        else:
            job.message = detail or job.message
        job.stats = stats
        session.add(job)
        session.commit()

    return _cb


def _run_inline(session: Session, job_id: int, paper_id: str | None, limit: int) -> None:
    from carrel.main import app_config

    job = session.get(Job, job_id)
    progress = _make_progress_cb(session, job_id)
    try:
        if job is not None:
            job.status = JobStatus.running.value
            job.started_at = datetime.now(UTC)
            session.add(job)
            session.commit()

        if paper_id:
            process_paper(session, app_config, paper_id, on_progress=progress)
            counts = {"parsed": 1, "failed": 0}
        else:
            counts = process_pending(
                session, app_config, limit=limit, on_progress=progress
            )
        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            job.stats = {
                **(job.stats or {}),
                **counts,
                "stage": "done",
                "detail": "Done.",
            }
            job.message = "ok"
            session.add(job)
            session.commit()
    except ProcessError as e:
        logger.info("process job %d failed: %s", job_id, e)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = str(e)
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("process job %d crashed", job_id)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"
            session.add(job)
            session.commit()


def _run_background(job_id: int, paper_id: str | None, limit: int) -> None:
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine

    engine = get_app_engine()
    with SqlSession(engine) as session:
        _run_inline(session, job_id, paper_id, limit)


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
