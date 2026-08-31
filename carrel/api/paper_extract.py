"""Per-paper LLM extraction endpoint (M8b foundation).

``POST /papers/extract`` runs the LLM over a single paper (``paper_id``)
or a batch of in-library parsed papers, extracting grounded concepts and
open questions into ``paper_concepts`` / ``paper_questions``.  Each paper
gets its own Job (kind ``paper_extract``) the frontend can poll.

Mirrors :mod:`carrel.api.summarize` and :mod:`carrel.api.topics`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, select

from carrel.api._job_progress import make_progress_cb
from carrel.api._job_io import job_to_out
from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus, Paper, PaperStatus
from carrel.pipeline.paper_extract import (
    PaperExtractError,
    extract_paper,
    select_stale_extract,
)
from carrel.schemas import JobOut, PaperExtractRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/papers/extract", tags=["paper_extract"])


@router.post("", response_model=list[JobOut])
def trigger_extract(
    body: PaperExtractRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> list[JobOut]:
    if body.paper_id:
        paper = session.get(Paper, body.paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="paper not found")
        targets = [paper]
    else:
        if body.force:
            # Force mode walks the parsed/summarized set directly (same
            # shape as summarize/topics), bypassing the staleness filter.
            stmt = (
                select(Paper)
                .where(
                    Paper.in_library.is_(True),
                    Paper.md_path.is_not(None),
                    Paper.status.in_([
                        PaperStatus.parsed.value,
                        PaperStatus.summarized.value,
                        PaperStatus.ready.value,
                    ]),
                )
                .order_by(Paper.created_at.desc())
                .limit(body.limit)
            )
            targets = list(session.exec(stmt).all())
        else:
            targets = select_stale_extract(session, limit=body.limit)

    if not targets:
        return []

    now = datetime.now(UTC)
    jobs: list[Job] = []
    for paper in targets:
        job = Job(
            kind=JobKind.paper_extract.value,
            status=JobStatus.queued.value,
            message=f"Queued — {paper.title[:80]}",
            stats={
                "paper_id": paper.id,
                "paper_title": paper.title,
                "stage": "queued",
                "detail": "Queued…",
                "deep": body.deep,
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
        background.add_task(
            _run_all_background,
            [j.id for j in jobs if j.id is not None],
            paper_ids,
            force,
            body.deep,
        )
    else:
        _run_all(
            session,
            [j.id for j in jobs if j.id is not None],
            paper_ids,
            force,
            body.deep,
        )
        for j in jobs:
            session.refresh(j)
    return [job_to_out(j) for j in jobs]


def _make_progress_cb(session: Session, job_id: int):
    return make_progress_cb(session, job_id, default_stage="paper_extract")


def _run_all(
    session: Session,
    job_ids: list[int],
    paper_ids: list[str],
    force: bool,
    deep: bool,
) -> None:
    from carrel.main import app_config
    assert len(job_ids) == len(paper_ids)
    for job_id, paper_id in zip(job_ids, paper_ids, strict=True):
        _run_one(session, job_id, paper_id, app_config, force, deep)


def _run_one(
    session: Session,
    job_id: int,
    paper_id: str,
    cfg,
    force: bool,
    deep: bool,
) -> None:
    job = session.get(Job, job_id)
    progress = _make_progress_cb(session, job_id)
    try:
        if job is not None:
            job.status = JobStatus.running.value
            job.started_at = datetime.now(UTC)
            session.add(job)
            session.commit()
        extract_paper(session, cfg, paper_id, deep=deep, force=force, on_progress=progress)
        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            job.stats = {**(job.stats or {}), "stage": "done", "detail": "Done."}
            job.message = "Done"
            session.add(job)
            session.commit()
    except PaperExtractError as e:
        logger.info("paper extract job %d failed: %s", job_id, e)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = str(e)[:200]
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("paper extract job %d crashed", job_id)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"[:200]
            session.add(job)
            session.commit()


def _run_all_background(
    job_ids: list[int], paper_ids: list[str], force: bool, deep: bool
) -> None:
    from sqlmodel import Session as SqlSession
    from carrel.db import get_app_engine

    engine = get_app_engine()
    with SqlSession(engine) as session:
        _run_all(session, job_ids, paper_ids, force, deep)

