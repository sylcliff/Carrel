"""Embedding endpoint (M5): chunk + embed parsed papers.

``POST /embed`` runs one paper (``paper_id``) or a batch of parsed papers
through ``parsed -> ready``. Each paper gets its own Job (kind ``embed``)
that the frontend can poll for progress, mirroring :mod:`carrel.api.process`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session

from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus, Paper
from carrel.pipeline.embed import EmbedError, embed_paper
from carrel.schemas import EmbedRequest, JobOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/embed", tags=["embed"])


@router.post("", response_model=list[JobOut])
def trigger_embed(
    body: EmbedRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> list[JobOut]:
    if body.paper_id:
        paper = session.get(Paper, body.paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="paper not found")
        targets = [paper]
    else:
        from carrel.pipeline.embed import select_pending_embed
        targets = select_pending_embed(session, limit=body.limit)

    if not targets:
        return []

    now = datetime.now(UTC)
    jobs: list[Job] = []
    for paper in targets:
        job = Job(
            kind=JobKind.embed.value,
            status=JobStatus.queued.value,
            message=f"Queued — {paper.title[:80]}",
            stats={
                "paper_id": paper.id,
                "paper_title": paper.title,
                "stage": "queued",
                "detail": "Queued…",
            },
            created_at=now,
        )
        session.add(job)
        jobs.append(job)
    session.flush()
    job_ids = [j.id for j in jobs if j.id is not None]
    session.commit()
    for j in jobs:
        session.refresh(j)

    paper_ids = [p.id for p in targets]
    if body.background:
        background.add_task(_run_all_background, job_ids, paper_ids)
    else:
        _run_all(session, job_ids, paper_ids)
        for j in jobs:
            session.refresh(j)
    return [_to_out(j) for j in jobs]


def _make_progress_cb(session: Session, job_id: int):
    def _cb(progress: dict) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        stage = progress.get("stage", "embed")
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


def _run_all(session: Session, job_ids: list[int], paper_ids: list[str]) -> None:
    from carrel.main import app_config
    assert len(job_ids) == len(paper_ids)
    for job_id, paper_id in zip(job_ids, paper_ids, strict=True):
        _run_one(session, job_id, paper_id, app_config)


def _run_one(session: Session, job_id: int, paper_id: str, cfg) -> None:
    job = session.get(Job, job_id)
    progress = _make_progress_cb(session, job_id)
    try:
        if job is not None:
            job.status = JobStatus.running.value
            job.started_at = datetime.now(UTC)
            session.add(job)
            session.commit()
        embed_paper(session, cfg, paper_id, on_progress=progress)
        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            job.stats = {**(job.stats or {}), "stage": "done", "detail": "Done."}
            job.message = "Done"
            session.add(job)
            session.commit()
    except EmbedError as e:
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = str(e)[:200]
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("embed job %d crashed", job_id)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"[:200]
            session.add(job)
            session.commit()


def _run_all_background(job_ids: list[int], paper_ids: list[str]) -> None:
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine
    from carrel.main import app_config

    engine = get_app_engine()
    with SqlSession(engine) as session:
        for job_id, paper_id in zip(job_ids, paper_ids, strict=True):
            _run_one(session, job_id, paper_id, app_config)


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
