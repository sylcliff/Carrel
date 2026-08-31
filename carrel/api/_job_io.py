"""Shared Job <-> JobOut conversion.

Almost every router (sync, citations, embed, summarize, process, …) has a
private ``_to_out(r: Job) -> JobOut`` that maps a SQLModel Job row to its
public Pydantic shape with the same eight fields. Keeping a single
implementation here means a future column (e.g. ``owner``) is added in one
place, not eleven.
"""
from __future__ import annotations

from carrel.models import Job
from carrel.schemas import JobOut


def job_to_out(r: Job) -> JobOut:
    """Serialize a Job row to its API representation.

    SQLModel's ``id`` is None until the row is flushed; coerce to 0 so the
    shape matches a persisted row. Callers that have already asserted the
    id is set can pass through; downstream consumers don't care.
    """
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
