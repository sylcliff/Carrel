"""Manual refresh endpoint for a scholar's OpenAlex works list.

Mirrors :mod:`carrel.api.authors_backfill` (Job per action,
``BackgroundTasks`` worker, ``_make_progress_cb``) and is paired with
:mod:`carrel.api.scholars`, whose ``list_scholar_works`` lazily kicks
off the same sync on first visit. The refresh route is the explicit
"Refresh from OpenAlex" path the user triggers from the scholar page
header.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlmodel import Session

from carrel.db import get_session_dep
from carrel.models import AuthorWorksSync, Job, JobKind, JobStatus
from carrel.pipeline.scholar_works_sync import sync_scholar_works
from carrel.pipeline.wiki._scholars_agg import NAME_KEY_PREFIX
from carrel.schemas import JobOut, ScholarSyncStatusOut
from carrel.api.scholars import _get_scholars

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scholars", tags=["scholars"])


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


def _resolve_aid(session: Session, key: str) -> str:
    """Validate the key is an A-ID scholar in the local aggregation.

    Returns the A-ID. Raises 404 if the scholar is not addressable, or
    422 if the key is a name-only entry (``name:...`` prefix) which
    has no OpenAlex author identity to fetch.
    """
    summary = next((s for s in _get_scholars(session) if s.key == key), None)
    if summary is None:
        raise HTTPException(status_code=404, detail="scholar not found in library")
    if key.startswith(NAME_KEY_PREFIX):
        raise HTTPException(
            status_code=422,
            detail=(
                "This author is matched by name only and has no OpenAlex "
                "Author ID. Run 'Resolve authors' from the Scholars page "
                "to look one up, then revisit this profile."
            ),
        )
    return key


def _run_refresh_job(job_id: int, aid: str) -> None:
    """Worker body for the manual refresh path.

    Opens a fresh session (mirrors :mod:`carrel.api.scholar_dedup`'s
    ``_run_bg``); the request session is closed by the time the worker
    runs.
    """
    from sqlmodel import Session as SqlSession
    from carrel.db import get_app_engine

    engine = get_app_engine()
    with SqlSession(engine) as sess:
        j = sess.get(Job, job_id)

        def _cb(p: dict[str, Any]) -> None:
            jj = sess.get(Job, job_id)
            if jj is None:
                return
            stats = {**(jj.stats or {})}
            for key_name in (
                "stage", "detail", "pages", "works", "total",
                "scholar_aid", "result",
            ):
                if key_name in p:
                    stats[key_name] = p[key_name]
            jj.stats = stats
            detail = p.get("detail") or ""
            if detail:
                jj.message = detail[:200]
            sess.add(jj)
            sess.commit()

        try:
            if j is not None:
                j.status = JobStatus.running.value
                j.started_at = datetime.now(UTC)
                sess.add(j)
                sess.commit()

            result = sync_scholar_works(sess, aid, on_progress=_cb, force=True)
            if j is not None:
                j.status = JobStatus.done.value
                j.finished_at = datetime.now(UTC)
                j.stats = {
                    **(j.stats or {}),
                    "stage": "done",
                    "result": result.get("status", "ready"),
                    "pages": result.get("pages", 0),
                    "works": result.get("works", 0),
                    "total": result.get("total"),
                }
                j.message = (
                    f"Refreshed {result.get('works', 0)} works "
                    f"across {result.get('pages', 0)} pages"
                )
                sess.add(j)
                sess.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception("scholar_works_sync job %d crashed", job_id)
            jj = sess.get(Job, job_id)
            if jj is not None:
                jj.status = JobStatus.failed.value
                jj.finished_at = datetime.now(UTC)
                jj.message = f"{type(e).__name__}: {e}"[:200]
                sess.add(jj)
                sess.commit()


@router.post("/{key}/sync/refresh", response_model=JobOut)
def refresh_scholar_works(
    key: str,
    background: bool = Query(
        True,
        description="Run in a worker (default) or block until done",
    ),
    bg: BackgroundTasks = ...,  # type: ignore[assignment]
    session: Session = Depends(get_session_dep),
) -> JobOut:
    """Force-refetch the OpenAlex works list for an A-ID scholar."""
    aid = _resolve_aid(session, key)

    now = datetime.now(UTC)
    job = Job(
        kind=JobKind.scholar_works_sync.value,
        status=JobStatus.queued.value,
        message=f"Queued - refresh works for {aid}",
        stats={
            "scholar_aid": aid,
            "stage": "queued",
            "detail": "Queued...",
            "pages": 0,
            "works": 0,
        },
        created_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    job_id = job.id
    assert job_id is not None

    if background:
        bg.add_task(_run_refresh_job, job_id, aid)
    else:
        # Block on the same path so the response carries the final
        # counts. Reuse the request session; a slow sync could starve
        # other requests, but ``background=False`` is only used by
        # tests + curl.
        _run_refresh_job(job_id, aid)
        session.refresh(job)

    return _to_out(job)


@router.get("/{key}/sync_status", response_model=ScholarSyncStatusOut)
def get_scholar_sync_status(
    key: str,
    session: Session = Depends(get_session_dep),
) -> ScholarSyncStatusOut:
    """Cheap polling endpoint for the scholar page's "Loading..." state."""
    aid = _resolve_aid(session, key)
    row = session.get(AuthorWorksSync, aid)
    if row is None:
        return ScholarSyncStatusOut(author_id=aid, status="missing")
    return ScholarSyncStatusOut(
        author_id=aid,
        status=row.status,
        total_count=row.total_count,
        last_full_sync_at=row.last_full_sync_at,
        last_error=row.last_error,
    )
