"""Scheduler status + schedule settings endpoints.

`GET  /schedule`    — list configured cron jobs, next-run time, last result.
`PATCH /schedule`   — flip enable switches or change cron strings. Writes back
                      to the active YAML config and restarts the in-process
                      scheduler. Safe for single-user self-hosted Carrel.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from carrel import scheduler as sched_mod
from carrel.config import CarrelYAML, load_yaml
from carrel.config_store import update_schedule
from carrel.db import get_session_dep
from carrel.schemas import ScheduledRunAck, SchedulerStatus, SchedulerUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedule", tags=["schedule"])


def _get_app_config() -> CarrelYAML:
    # Late import to avoid the circular main -> api -> main chain.
    from carrel.main import app_config, CONFIG_PATH  # noqa: PLC0415

    return app_config, CONFIG_PATH


@router.get("", response_model=SchedulerStatus)
def get_schedule(session: Session = Depends(get_session_dep)) -> SchedulerStatus:
    cfg, _ = _get_app_config()
    return SchedulerStatus.model_validate(sched_mod.get_status(cfg, session))


@router.patch("", response_model=SchedulerStatus)
def patch_schedule(
    body: SchedulerUpdate,
    session: Session = Depends(get_session_dep),
) -> SchedulerStatus:
    cfg, config_path = _get_app_config()
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return SchedulerStatus.model_validate(sched_mod.get_status(cfg, session))

    path = Path(config_path)
    try:
        update_schedule(path, updates)
    except ValueError as e:
        # Pydantic / cron parse errors — user fixable, so 400 not 500.
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as e:
        logger.exception("failed to write %s", path)
        raise HTTPException(status_code=500, detail=f"could not write config: {e}") from e

    # Reload the YAML so in-memory config matches what was just persisted,
    # then restart the scheduler with the new settings. Only the schedule
    # block is mutated — other routers holding a reference to `app_config`
    # see the updated values because Pydantic models are mutable in place.
    new_cfg = load_yaml(path)
    cfg.schedule = new_cfg.schedule
    sched_mod.restart_scheduler(cfg)

    return SchedulerStatus.model_validate(sched_mod.get_status(cfg, session))


@router.post("/{job_id}/run", response_model=ScheduledRunAck)
def run_job_now(job_id: str) -> ScheduledRunAck:
    """Immediately dispatch one of the known scheduled jobs, regardless of
    whether it's currently enabled or when its next cron tick is.

    Runs in a background thread; the existing Job row machinery in each job
    body is what the UI polls for progress. Returns 404 for an unknown id
    and 409 when a hard prerequisite (e.g. SSH) is missing.
    """
    spec = sched_mod.get_job_spec(job_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown scheduled job: {job_id}")

    dispatched, message = sched_mod.run_job_now(spec)
    if not dispatched:
        # 409 for "can't run right now" — missing prerequisite or already running.
        raise HTTPException(status_code=409, detail=message)
    return ScheduledRunAck(job_id=job_id, running=True, message=message)
