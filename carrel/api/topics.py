"""Topic classification endpoint.

``POST /topics`` classifies one paper (``paper_id``) or a batch of in-library
papers with no topics, assigning 1-4 LLM-generated research themes. Each paper
gets its own Job (kind ``topics``) the frontend can poll for progress,
mirroring :mod:`carrel.api.summarize`.

``GET /topics`` lists every topic with the number of papers carrying it, for
the sidebar facet and the Topics browse page.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy import func
from sqlmodel import Session, select

from carrel.db import get_session_dep
from carrel.api._app_cache import cached
from carrel.api._invalidation import invalidate_topics_recomputed, invalidate_paper_mutated
from carrel.models import Job, JobKind, JobStatus, Paper, PaperTopic, Topic
from carrel.pipeline.topics import TopicsError, topics_paper
from carrel.schemas import JobOut, TopicWithCount, TopicsRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/topics", tags=["topics"])


@router.post("", response_model=list[JobOut])
def trigger_topics(
    body: TopicsRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> list[JobOut]:
    if body.paper_id:
        paper = session.get(Paper, body.paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="paper not found")
        targets = [paper]
    else:
        from carrel.pipeline.topics import select_pending_topics
        targets = select_pending_topics(session, limit=body.limit)

    if not targets:
        return []

    now = datetime.now(UTC)
    jobs: list[Job] = []
    for paper in targets:
        job = Job(
            kind=JobKind.topics.value,
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
        background.add_task(
            _run_all_background, [j.id for j in jobs if j.id is not None], paper_ids, force
        )
    else:
        _run_all(session, [j.id for j in jobs if j.id is not None], paper_ids, force)
        for j in jobs:
            session.refresh(j)
    return [_to_out(j) for j in jobs]


@cached("topics", tags=("topics", "papers_list"))
def _list_topics_body(session: Session) -> list[TopicWithCount]:
    """Cached topic counts. Invalidated on classifier commit / paper writes."""
    rows = session.exec(
        select(
            Topic.id,
            Topic.name,
            Topic.description,
            func.count(PaperTopic.paper_id),
        )
        .join(PaperTopic, PaperTopic.topic_id == Topic.id)
        .join(Paper, Paper.id == PaperTopic.paper_id)
        .where(Paper.in_library.is_(True), Paper.discarded.is_(False))
        .group_by(Topic.id, Topic.name, Topic.description)
        .order_by(func.count(PaperTopic.paper_id).desc(), Topic.name)
    ).all()
    return [
        TopicWithCount(
            id=t_id, name=name, description=description, paper_count=count or 0
        )
        for t_id, name, description, count in rows
    ]


@router.get("", response_model=list[TopicWithCount])
def list_topics(
    response: Response,
    session: Session = Depends(get_session_dep),
) -> list[TopicWithCount]:
    """All topics with the number of in-library papers carrying each.

    Topics with zero papers are excluded (a topic only exists once the
    classifier assigns it, but papers can be deleted afterward). Ordered by
    descending paper count, then name.

    Layer 1: counts are an aggregation over the library + topic
    assignments. A precise ETag is not practical; use a short
    max-age and rely on L2 invalidation (Phase 3) fired from
    ``_run_one`` after the classifier commits.
    """
    response.headers["Cache-Control"] = "private, max-age=10, stale-while-revalidate=30"
    return _list_topics_body(session)


def _make_progress_cb(session: Session, job_id: int):
    def _cb(progress: dict) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        stage = progress.get("stage", "topics")
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
        topics_paper(session, cfg, paper_id, force=force, on_progress=progress)
        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            job.stats = {**(job.stats or {}), "stage": "done", "detail": "Done."}
            job.message = "Done"
            session.add(job)
            session.commit()
    except TopicsError as e:
        logger.info("topics job %d failed: %s", job_id, e)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = str(e)[:200]
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("topics job %d crashed", job_id)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"[:200]
            session.add(job)
            session.commit()
    else:
        # L2: classifier finished successfully — drop the global topics
        # cache and the per-paper list. A failed run leaves the cache
        # untouched (the existing topics are still valid).
        invalidate_topics_recomputed()
        invalidate_paper_mutated(paper_id, mutate={"status"})


def _run_all_background(job_ids: list[int], paper_ids: list[str], force: bool) -> None:
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine

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
