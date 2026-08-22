"""Per-paper endpoint: check an arXiv paper for a published journal version."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session

from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus, Paper
from carrel.schemas import JobOut, PublicationCheckRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/papers", tags=["publication"])


def _to_out(job: Job) -> JobOut:
    assert job.id is not None
    return JobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        message=job.message,
        stats=job.stats,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
    )


@router.post("/{paper_id}/check-publication", response_model=JobOut)
def check_publication(
    paper_id: str,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
    body: PublicationCheckRequest = PublicationCheckRequest(),
) -> JobOut:
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")
    if not paper.arxiv_id:
        raise HTTPException(
            status_code=400, detail="paper has no arXiv id to check for a journal version"
        )

    now = datetime.now(UTC)
    job = Job(
        kind=JobKind.publication_check.value,
        status=JobStatus.queued.value,
        message=f"Queued — {paper.title[:80]}",
        stats={
            "paper_id": paper.id,
            "paper_title": paper.title,
            "stage": "queued",
            "detail": "Queued…",
            "force": body.force,
        },
        created_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    assert job.id is not None

    if body.background and background is not None:
        background.add_task(_run_background, job.id, paper.id, body.force)
    else:
        _run(session, job.id, paper.id, body.force)
        session.refresh(job)

    return _to_out(job)


def _make_progress_cb(session: Session, job_id: int):
    def _cb(progress: dict) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        detail = progress.get("detail", "")
        title = progress.get("paper_title") or progress.get("title") or ""
        stats = {**(job.stats or {})}
        for key in ("stage", "detail", "paper_id", "paper_title", "journal_doi"):
            if key in progress:
                stats[key] = progress[key]
        job.stats = stats
        job.message = f"{title} — {detail}" if (title and detail) else (detail or title or job.message)
        session.add(job)
        session.commit()
    return _cb


def _run(session: Session, job_id: int, paper_id: str, force: bool) -> None:
    from carrel.main import app_config
    from carrel.pipeline.publication_check import check_and_apply

    job = session.get(Job, job_id)
    paper = session.get(Paper, paper_id)
    base_cb = _make_progress_cb(session, job_id)
    title = paper.title if paper is not None else ""

    def progress(p: dict) -> None:
        base_cb({**p, "paper_id": paper_id, "paper_title": p.get("paper_title") or title})

    try:
        if job is not None:
            job.status = JobStatus.running.value
            job.started_at = datetime.now(UTC)
            session.add(job)
            session.commit()

        check_and_apply(session, app_config, paper_id, force=force, on_progress=progress)

        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            job.stats = {**(job.stats or {}), "stage": "done", "detail": "Done."}
            job.message = "Done"
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("publication check job %d failed", job_id)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"[:200]
            session.add(job)
            session.commit()


def _run_background(job_id: int, paper_id: str, force: bool) -> None:
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine

    engine = get_app_engine()
    with SqlSession(engine) as session:
        _run(session, job_id, paper_id, force)
