"""Agent run / step query endpoints (M17).

Read-only access to the execution trace recorded by
:mod:`carrel.agent_recorder`. The /agent page consumes these to show
each pipeline's recent runs and the per-step timeline of any one run.

Endpoints
---------

* ``GET /agent/runs`` — list runs, optionally filtered by ``pipeline_id``
  and/or ``status``. Newest first.
* ``GET /agent/runs/{id}`` — one run with all its steps (in seq order).
* ``GET /agent/pipelines`` — list every pipeline id that has at least
  one recorded run, with the most recent run's status. Drives the
  /agent "Pipelines" sidebar.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, func, select

from carrel.agent_recorder import pipeline_display_name
from carrel.db import get_session_dep
from carrel.models import AgentRun, AgentStep

router = APIRouter(prefix="/agent", tags=["agent"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AgentStepOut(BaseModel):
    id: int
    seq: int
    node_id: str | None
    label: str
    kind: str
    feature: str | None
    status: str
    error: str | None
    input_summary: str | None
    output_summary: str | None
    detail: dict[str, Any] | None
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


class AgentRunOut(BaseModel):
    id: int
    pipeline_id: str
    pipeline_name: str
    status: str
    trigger: str
    context: dict[str, Any] | None
    summary: dict[str, Any] | None
    error: str | None
    job_id: int | None
    paper_id: str | None
    subject: str | None
    step_count: int
    success_count: int
    failed_count: int
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    created_at: datetime


class AgentRunDetail(AgentRunOut):
    steps: list[AgentStepOut]


class AgentPipelineSummary(BaseModel):
    pipeline_id: str
    pipeline_name: str
    run_count: int
    last_status: str | None
    last_started_at: datetime | None
    last_run_id: int | None
    last_subject: str | None


# ---------------------------------------------------------------------------
# Row -> schema
# ---------------------------------------------------------------------------


def _step_to_out(row: AgentStep) -> AgentStepOut:
    duration_ms: int | None = row.duration_ms
    if duration_ms is None and row.finished_at and row.started_at:
        duration_ms = max(
            0,
            int((row.finished_at - row.started_at).total_seconds() * 1000),
        )
    return AgentStepOut(
        id=row.id or 0,
        seq=row.seq,
        node_id=row.node_id,
        label=row.label,
        kind=row.kind,
        feature=row.feature,
        status=row.status,
        error=row.error,
        input_summary=row.input_summary,
        output_summary=row.output_summary,
        detail=row.detail,
        model=row.model,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        total_tokens=row.total_tokens,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=duration_ms,
    )


def _run_to_out(row: AgentRun) -> AgentRunOut:
    duration_ms: int | None = None
    if row.finished_at and row.started_at:
        duration_ms = max(
            0,
            int((row.finished_at - row.started_at).total_seconds() * 1000),
        )
    return AgentRunOut(
        id=row.id or 0,
        pipeline_id=row.pipeline_id,
        pipeline_name=row.pipeline_name,
        status=row.status,
        trigger=row.trigger,
        context=row.context,
        summary=row.summary,
        error=row.error,
        job_id=row.job_id,
        paper_id=row.paper_id,
        subject=row.subject,
        step_count=row.step_count,
        success_count=row.success_count,
        failed_count=row.failed_count,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=duration_ms,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=list[AgentRunOut])
def list_runs(
    session: Session = Depends(get_session_dep),
    pipeline_id: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=16),
    paper_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[AgentRunOut]:
    """List agent runs, newest first. Optional filters by pipeline/status/paper."""
    stmt = select(AgentRun).order_by(AgentRun.id.desc()).limit(limit)
    if pipeline_id:
        stmt = stmt.where(AgentRun.pipeline_id == pipeline_id)
    if status:
        stmt = stmt.where(AgentRun.status == status)
    if paper_id:
        stmt = stmt.where(AgentRun.paper_id == paper_id)
    rows = session.exec(stmt).all()
    return [_run_to_out(r) for r in rows]


@router.get("/runs/{run_id}", response_model=AgentRunDetail)
def get_run(
    run_id: int,
    session: Session = Depends(get_session_dep),
) -> AgentRunDetail:
    """One run with its full step timeline (oldest step first)."""
    row = session.get(AgentRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    steps = session.exec(
        select(AgentStep)
        .where(AgentStep.run_id == run_id)
        .order_by(AgentStep.seq.asc())
    ).all()
    base = _run_to_out(row)
    return AgentRunDetail(
        **base.model_dump(),
        steps=[_step_to_out(s) for s in steps],
    )


@router.get("/pipelines", response_model=list[AgentPipelineSummary])
def list_pipeline_summaries(
    session: Session = Depends(get_session_dep),
) -> list[AgentPipelineSummary]:
    """Pipelines that have at least one recorded run, with most-recent stats.

    The static catalog from :data:`carrel.agent_recorder.PIPELINE_CATALOG`
    is the source of truth for the known pipelines, but this endpoint
    only returns pipelines that have actually been executed. The /agent
    page merges this list with the catalog at render time so an unknown
    pipeline id (e.g. one added in a future version) still shows up in
    runs even if the catalog didn't ship a card for it.
    """
    # Pull the latest run per pipeline_id. A simple "max(id) per
    # pipeline" subquery works on both SQLite and Postgres without
    # relying on DISTINCT ON.
    latest_id_subq = (
        select(AgentRun.pipeline_id, func.max(AgentRun.id).label("max_id"))
        .group_by(AgentRun.pipeline_id)
        .subquery()
    )
    rows = session.exec(
        select(AgentRun)
        .join(
            latest_id_subq,
            (AgentRun.pipeline_id == latest_id_subq.c.pipeline_id)
            & (AgentRun.id == latest_id_subq.c.max_id),
        )
        .order_by(AgentRun.pipeline_id.asc())
    ).all()
    # Run counts per pipeline (cheap single grouped query).
    count_rows = session.exec(
        select(AgentRun.pipeline_id, func.count(AgentRun.id))
        .group_by(AgentRun.pipeline_id)
    ).all()
    counts = {pid: int(c) for pid, c in count_rows}

    out: list[AgentPipelineSummary] = []
    for r in rows:
        out.append(
            AgentPipelineSummary(
                pipeline_id=r.pipeline_id,
                pipeline_name=r.pipeline_name
                or pipeline_display_name(r.pipeline_id),
                run_count=counts.get(r.pipeline_id, 0),
                last_status=r.status,
                last_started_at=r.started_at,
                last_run_id=r.id,
                last_subject=r.subject,
            )
        )
    return out


__all__ = [
    "router",
    "AgentRunOut",
    "AgentRunDetail",
    "AgentStepOut",
    "AgentPipelineSummary",
]
