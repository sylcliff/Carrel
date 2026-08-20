"""Summarization endpoint (M4): generate LLM TL;DR/abstract/keywords.

``POST /summarize`` runs one paper (``paper_id``) or a batch of parsed papers
missing summary fields through ``parsed -> summarized``. Each paper gets its
own Job (kind ``summarize``) that the frontend can poll for progress,
mirroring :mod:`carrel.api.embed`. Summarize failures are non-fatal at the
pipeline level (the paper stays ``parsed``) but the corresponding Job is
marked failed so the UI surfaces the reason (e.g. missing API key).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session

from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus, Paper
from carrel.pipeline.summarize import SummarizeError, summarize_paper
from carrel.schemas import JobOut, SummarizeRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/summarize", tags=["summarize"])


@router.post("", response_model=list[JobOut])
def trigger_summarize(
    body: SummarizeRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> list[JobOut]:
    if body.paper_id:
        paper = session.get(Paper, body.paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="paper not found")
        targets = [paper]
    else:
        from carrel.pipeline.summarize import select_pending_summarize
        targets = select_pending_summarize(session, limit=body.limit)

    if not targets:
        return []

    now = datetime.now(UTC)
    jobs: list[Job] = []
    for paper in targets:
        job = Job(
            kind=JobKind.summarize.value,
            status=JobStatus.queued.value,
            message=f"Queued — {paper.title[:80]}",
            stats={
                "paper_id": paper.id,
                "paper_title": paper.title,
                "stage": "queued",
                "detail": "Queued…",
                "force": body.force and bool(body.paper_id),
            },
            created_at=now,
        )
        session.add(job)
        jobs.append(job)
    session.flush()
    session.commit()
    for j in jobs:
        session.refresh(j)

    paper_ids = [p.id for p in targets]
    force = body.force and bool(body.paper_id)
    if body.background:
        background.add_task(_run_all_background, [j.id for j in jobs if j.id is not None], paper_ids, force)
    else:
        _run_all(session, [j.id for j in jobs if j.id is not None], paper_ids, force)
        for j in jobs:
            session.refresh(j)
    return [_to_out(j) for j in jobs]


def _make_progress_cb(session: Session, job_id: int):
    def _cb(progress: dict) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        stage = progress.get("stage", "summarize")
        detail = progress.get("detail", "")
        title = progress.get("paper_title") or ""
        stats = {**(job.stats or {})}
        stats["stage"] = stage
        stats["detail"] = detail
        if "paper_id" in progress:
            stats["paper_id"] = progress["paper_id"]
        if "paper_title" in progress:
            stats["paper_title"] = progress["paper_title"]
        job.stats = stats
        job.message = f"{title} — {detail}" if (title and detail) else (detail or job.message)
        session.add(job)
        session.commit()
    return _cb


def _run_all(session: Session, job_ids: list[int], paper_ids: list[str], force: bool) -> None:
    from carrel.main import app_config
    assert len(job_ids) == len(paper_ids)
    for job_id, paper_id in zip(job_ids, paper_ids, strict=True):
        _run_one(session, job_id, paper_id, app_config, force)


def _run_one(session: Session, job_id: int, paper_id: str, cfg, force: bool) -> None:
    job = session.get(Job, job_id)
    progress = _make_progress_cb(session, job_id)
    try:
        if job is not None:
            job.status = JobStatus.running.value
            job.started_at = datetime.now(UTC)
            session.add(job)
            session.commit()
        summarize_paper(session, cfg, paper_id, force=force, on_progress=progress)
        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            job.stats = {**(job.stats or {}), "stage": "done", "detail": "Done."}
            job.message = "Done"
            session.add(job)
            session.commit()
    except SummarizeError as e:
        logger.info("summarize job %d failed: %s", job_id, e)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = str(e)[:200]
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("summarize job %d crashed", job_id)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"[:200]
            session.add(job)
            session.commit()


def _run_all_background(job_ids: list[int], paper_ids: list[str], force: bool) -> None:
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine
    from carrel.main import app_config

    engine = get_app_engine()
    with SqlSession(engine) as session:
        _run_all(session, job_ids, paper_ids, force)


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
