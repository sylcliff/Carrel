"""APScheduler wrapper for the daily sync (M6).

Single-process, in-process scheduler. Reads `schedule.enabled` and
`schedule.sync_cron` from YAML. The cron expression is standard 5-field
user crontab format (minute hour dom month dow), parsed via APScheduler's
`from_crontab`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session

from carrel.config import CarrelYAML
from carrel.db import get_app_engine
from carrel.models import Job, JobKind, JobStatus
from carrel.pipeline.runner import run_sync

logger = logging.getLogger("carrel.scheduler")

# Daily job: cover a bit more than 24h so a delayed/skipped run still sweeps.
DEFAULT_LOOKBACK_HOURS = 26

_scheduler: AsyncIOScheduler | None = None


def _scheduled_sync(lookback_hours: int) -> None:
    """Body of the cron job — opens its own DB session (runs in a worker thread)."""
    # Import lazily so the module stays importable without app config.
    from carrel.main import app_config  # noqa: PLC0415

    engine = get_app_engine()
    with Session(engine) as session:
        job = Job(
            kind=JobKind.sync.value,
            status=JobStatus.running.value,
            message=f"scheduled (lookback_hours={lookback_hours})",
            started_at=datetime.now(UTC),
            stats={"lookback_hours": lookback_hours, "sources": None, "scheduled": True},
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        try:
            run_sync(session, app_config, lookback_hours=lookback_hours, job=job)
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("scheduled sync failed: %s", e)
            job.status = JobStatus.failed.value
            job.message = f"{type(e).__name__}: {e}"[:500]
            job.finished_at = datetime.now(UTC)
            session.add(job)
            session.commit()


def start_scheduler(cfg: CarrelYAML) -> AsyncIOScheduler | None:
    """Start the scheduler if enabled. Idempotent — returns existing instance."""
    global _scheduler
    if not cfg.schedule.enabled:
        logger.info("scheduler disabled (schedule.enabled=false)")
        return None
    if _scheduler is not None:
        return _scheduler

    trigger = CronTrigger.from_crontab(cfg.schedule.sync_cron)
    scheduler = AsyncIOScheduler(daemon=True)
    scheduler.add_job(
        _scheduled_sync,
        trigger=trigger,
        args=[DEFAULT_LOOKBACK_HOURS],
        id="daily_sync",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("scheduler started: daily sync cron=%r", cfg.schedule.sync_cron)
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("scheduler stopped")
