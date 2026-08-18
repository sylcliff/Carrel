"""Citation endpoints: list stored citations and refresh them on demand.

``GET  /papers/{paper_id}/citations`` returns the stored citing-paper list with
each item's library-membership resolved (so the UI can link to papers already
in Carrel).

``POST /papers/{paper_id}/refresh-citations`` kicks off a Semantic Scholar
lookup as one Job (kind ``citations``), mirroring the one-job-per-paper design
of :mod:`carrel.api.process`. The frontend polls the job the same way it polls
a parse job.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, or_, select

from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus, Paper
from carrel.pipeline.citations import enrich_paper
from carrel.schemas import (
    CitationItem,
    CitationListOut,
    CitationRefreshRequest,
    JobOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/papers", tags=["citations"])


def _to_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id or 0,
        kind=job.kind,
        status=job.status,
        message=job.message,
        stats=job.stats,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
    )


def _resolve_library(
    session: Session, citing: list[dict]
) -> dict[str, str]:
    """Build identifier -> Carrel Paper.id lookups by DOI / arXiv / S2 id.

    One batched query keeps this cheap even for long citation lists. DOIs are
    stored as full URLs and compared case-insensitively against bare DOIs.
    """
    dois = {c["doi"].lower() for c in citing if c.get("doi")}
    arxiv_ids = {c["arxiv_id"] for c in citing if c.get("arxiv_id")}
    s2_ids = {c["s2_paper_id"] for c in citing if c.get("s2_paper_id")}

    by_id: dict[str, str] = {}
    if not (dois or arxiv_ids or s2_ids):
        return by_id

    clauses = []
    if dois:
        doi_variants = list(dois) + [f"https://doi.org/{d}" for d in dois]
        clauses.append(Paper.doi.in_(doi_variants))
    if arxiv_ids:
        clauses.append(Paper.arxiv_id.in_(list(arxiv_ids)))
    if s2_ids:
        clauses.append(Paper.s2_paper_id.in_(list(s2_ids)))

    for p in session.exec(select(Paper).where(or_(*clauses))).all():
        if p.doi:
            bare = p.doi.lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
            by_id[f"doi:{bare}"] = p.id
        if p.arxiv_id:
            by_id[f"arxiv:{p.arxiv_id}"] = p.id
        if p.s2_paper_id:
            by_id[f"s2:{p.s2_paper_id}"] = p.id
    return by_id


def _find_in_library(lib_map: dict[str, str], item: dict) -> str | None:
    if item.get("doi"):
        pid = lib_map.get(f"doi:{item['doi'].lower()}")
        if pid:
            return pid
    if item.get("arxiv_id"):
        pid = lib_map.get(f"arxiv:{item['arxiv_id']}")
        if pid:
            return pid
    if item.get("s2_paper_id"):
        pid = lib_map.get(f"s2:{item['s2_paper_id']}")
        if pid:
            return pid
    return None


@router.get("/{paper_id}/citations", response_model=CitationListOut)
def list_citations(
    paper_id: str,
    session: Session = Depends(get_session_dep),
) -> CitationListOut:
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")

    citing_raw = paper.citing_papers or []
    lib_map = _resolve_library(session, citing_raw)

    items: list[CitationItem] = []
    for c in citing_raw:
        pid = _find_in_library(lib_map, c)
        items.append(CitationItem(
            title=c.get("title"),
            year=c.get("year"),
            doi=c.get("doi"),
            arxiv_id=c.get("arxiv_id"),
            s2_paper_id=c.get("s2_paper_id"),
            in_library=pid is not None,
            paper_id=pid,
        ))

    from carrel.main import app_config

    cap = app_config.semantic_scholar.citations_limit
    return CitationListOut(
        paper_id=paper.id,
        citation_count=paper.citation_count,
        influential_citation_count=paper.influential_citation_count,
        reference_count=paper.reference_count,
        updated_at=paper.citations_updated_at,
        truncated=len(items) >= cap,
        citing=items,
    )


@router.post("/{paper_id}/refresh-citations", response_model=JobOut)
def refresh_citations(
    paper_id: str,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
    body: CitationRefreshRequest = CitationRefreshRequest(),
) -> JobOut:
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")

    now = datetime.now(UTC)
    job = Job(
        kind=JobKind.citations.value,
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
    session.commit()
    session.refresh(job)
    assert job.id is not None

    if body.background and background is not None:
        background.add_task(_run_background, job.id, paper.id)
    else:
        _run(session, job.id, paper.id)
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
        for key in ("stage", "detail", "paper_id", "paper_title"):
            if key in progress:
                stats[key] = progress[key]
        job.stats = stats
        job.message = f"{title} — {detail}" if (title and detail) else (detail or title or job.message)
        session.add(job)
        session.commit()
    return _cb


def _run(session: Session, job_id: int, paper_id: str) -> None:
    from carrel.main import app_config

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

        enrich_paper(session, app_config, paper_id, on_progress=progress)

        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            job.stats = {**(job.stats or {}), "stage": "done", "detail": "Done."}
            job.message = "Done"
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("citations job %d failed", job_id)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"[:200]
            session.add(job)
            session.commit()


def _run_background(job_id: int, paper_id: str) -> None:
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine

    engine = get_app_engine()
    with SqlSession(engine) as session:
        _run(session, job_id, paper_id)
