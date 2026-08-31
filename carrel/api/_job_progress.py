"""Shared helpers for the API layer's pipeline-endpoint progress reporting.

Five endpoints (citations / summarize / embed / paper_extract / topics) all
shaped the same way: a per-paper pipeline emits a progress dict, and the
endpoint wants to project that into ``Job.stats`` + ``Job.message`` so the
UI's job list can show live status.

Before this module each endpoint kept its own near-identical
``_make_progress_cb`` factory. The only meaningful variation was the
default stage name, so we collapsed them to one helper that takes the
stage as a parameter.
"""
from __future__ import annotations

from sqlmodel import Session

from carrel.models import Job


def make_progress_cb(
    session: Session, job_id: int, *, default_stage: str
) -> "callable":
    """Return a callback that updates ``Job.stats`` / ``Job.message``.

    The callback reads the job, merges the progress dict into ``stats``,
    builds a human-readable ``message``, and commits. If the job no
    longer exists (e.g. it was cancelled) the call is a silent no-op.

    ``default_stage`` is the stage name written into ``stats["stage"]``
    when the progress dict does not carry one — typically the name of
    the pipeline (e.g. ``"summarize"``, ``"embed"``).
    """
    def _cb(progress: dict) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        stage = progress.get("stage", default_stage)
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
        job.message = (
            f"{title} — {detail}"
            if (title and detail)
            else (detail or title or job.message)
        )
        session.add(job)
        session.commit()
    return _cb
