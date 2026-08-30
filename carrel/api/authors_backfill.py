"""Author backfill endpoint — resolve OpenAlex Author IDs for existing papers.

Papers imported from Semantic Scholar often store abbreviated author names
without an OpenAlex Author ID, which fragments the Scholars browse page.
``POST /authors-backfill`` finds in-library papers with unresolved authors and
a DOI/arXiv ID, then looks up each paper's canonical authorship from OpenAlex
and writes the A-IDs back.

Mirrors :mod:`carrel.api.topics`: each paper gets its own Job (kind
``authors_backfill``) the frontend polls for progress.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session

from carrel.db import get_session_dep
from carrel.api._job_io import job_to_out
from carrel.models import Job, JobKind, JobStatus, Paper
from carrel.pipeline.authors import backfill_paper, select_pending
from carrel.schemas import AuthorsBackfillRequest, JobOut
from carrel.sources.throttle import abort_if_throttled, openalex_throttle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/authors-backfill", tags=["authors-backfill"])


@router.post("", response_model=list[JobOut])
def trigger_backfill(
    body: AuthorsBackfillRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> list[JobOut]:
    if body.paper_id:
        paper = session.get(Paper, body.paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="paper not found")
        targets = [paper]
    else:
        targets = select_pending(session, limit=body.limit)

    if not targets:
        return []

    now = datetime.now(UTC)
    jobs: list[Job] = []
    for paper in targets:
        job = Job(
            kind=JobKind.authors_backfill.value,
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
    session.commit()
    for j in jobs:
        session.refresh(j)

    paper_ids = [p.id for p in targets]
    if body.background:
        background.add_task(
            _run_all_background, [j.id for j in jobs if j.id is not None], paper_ids
        )
    else:
        _run_all(session, [j.id for j in jobs if j.id is not None], paper_ids)
        for j in jobs:
            session.refresh(j)
    return [job_to_out(j) for j in jobs]


def _make_progress_cb(session: Session, job_id: int):
    def _cb(progress: dict) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        stats = {**(job.stats or {})}
        for key in ("stage", "detail", "paper_id", "paper_title", "filled", "skipped", "failed"):
            if key in progress:
                stats[key] = progress[key]
        job.stats = stats
        detail = progress.get("detail", "")
        title = progress.get("paper_title") or ""
        job.message = f"{title} — {detail}" if (title and detail) else (detail or job.message)
        session.add(job)
        session.commit()

    return _cb


def _run_all(session: Session, job_ids: list[int], paper_ids: list[str]) -> None:
    assert len(job_ids) == len(paper_ids)
    for job_id, paper_id in zip(job_ids, paper_ids, strict=True):
        # Bail out of the whole batch if OpenAlex is currently throttled —
        # 500 back-to-back calls would each pay 17 s of retry-wait for a
        # guarantee of failure. We mark the remaining queued jobs as failed
        # with a clear "throttled" result so the UI can render a different
        # banner than "data not in OpenAlex".
        msg = abort_if_throttled(
            openalex_throttle,
            on_abort=lambda m: _abort_remaining(
                session, job_ids, reason=f"throttled: {m}"
            ),
        )
        if msg is not None:
            return
        _run_one(session, job_id, paper_id)


def _abort_remaining(
    session: Session, job_ids: list[int], *, reason: str
) -> None:
    """Mark every still-pending job in this batch as failed/throttled.

    A 429 cooldown on OpenAlex means the rest of the batch cannot succeed
    until the budget resets, so we stop the loop early and surface a
    distinct ``result='throttled'`` so the Scholars page can show a
    rate-limit banner instead of "data not in OpenAlex".
    """
    now = datetime.now(UTC)
    for jid in job_ids:
        job = session.get(Job, jid)
        if job is None or job.status in (
            JobStatus.done.value,
            JobStatus.failed.value,
        ):
            continue
        job.status = JobStatus.failed.value
        job.finished_at = now
        job.message = f"Aborted: {reason}"
        stats = {**(job.stats or {}), "stage": "aborted", "result": "throttled"}
        job.stats = stats
        session.add(job)
    session.commit()
    logger.warning(
        "authors-backfill batch aborted: %d jobs marked throttled (%s)",
        len(job_ids),
        reason,
    )


def _run_one(session: Session, job_id: int, paper_id: str) -> None:
    job = session.get(Job, job_id)
    progress = _make_progress_cb(session, job_id)
    try:
        if job is not None:
            job.status = JobStatus.running.value
            job.started_at = datetime.now(UTC)
            session.add(job)
            session.commit()

        paper = session.get(Paper, paper_id)
        if paper is None:
            raise RuntimeError("paper vanished")
        status = backfill_paper(session, paper, on_progress=progress)

        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            job.stats = {**(job.stats or {}), "stage": "done", "result": status}
            job.message = f"Done ({status})"
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("authors-backfill job %d crashed", job_id)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"[:200]
            session.add(job)
            session.commit()


def _run_all_background(job_ids: list[int], paper_ids: list[str]) -> None:
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine

    engine = get_app_engine()
    with SqlSession(engine) as session:
        _run_all(session, job_ids, paper_ids)

