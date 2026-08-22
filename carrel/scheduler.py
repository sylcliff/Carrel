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


# Default batch sizes for the periodic sweeps.
DEFAULT_REMOTE_FILL_LIMIT = 50
DEFAULT_PUBLICATION_CHECK_LIMIT = 50


def _scheduled_remote_fill(limit: int) -> None:
    """Try to download PDFs for closed papers via the institutional host."""
    from carrel.main import app_config  # noqa: PLC0415
    from carrel.pipeline.publication_check import fill_closed_papers
    from carrel.sources import remote_downloader

    if not remote_downloader.is_configured():
        logger.info("scheduled remote_fill skipped: institutional SSH not configured")
        return

    engine = get_app_engine()
    with Session(engine) as session:
        job = Job(
            kind=JobKind.remote_fill.value,
            status=JobStatus.running.value,
            message="scheduled remote fill",
            started_at=datetime.now(UTC),
            stats={"limit": limit, "scheduled": True},
        )
        session.add(job)
        session.commit()
        try:
            counts = fill_closed_papers(session, app_config, limit=limit)
            job.status = JobStatus.done.value
            job.message = (
                f"candidates={counts.get('candidates', 0)} "
                f"parsed={counts.get('parsed', 0)} failed={counts.get('failed', 0)}"
            )
            job.stats = {**(job.stats or {}), **counts}
            job.finished_at = datetime.now(UTC)
            session.add(job)
            session.commit()
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("scheduled remote fill failed: %s", e)
            job.status = JobStatus.failed.value
            job.message = f"{type(e).__name__}: {e}"[:500]
            job.finished_at = datetime.now(UTC)
            session.add(job)
            session.commit()


def _scheduled_publication_check(limit: int) -> None:
    """Check old arXiv papers for a published journal version."""
    from carrel.main import app_config  # noqa: PLC0415
    from carrel.pipeline.publication_check import check_pending

    engine = get_app_engine()
    with Session(engine) as session:
        job = Job(
            kind=JobKind.publication_check.value,
            status=JobStatus.running.value,
            message="scheduled publication check",
            started_at=datetime.now(UTC),
            stats={"limit": limit, "scheduled": True},
        )
        session.add(job)
        session.commit()
        try:
            counts = check_pending(session, app_config, limit=limit)
            job.status = JobStatus.done.value
            job.message = (
                f"candidates={counts.get('candidates', 0)} "
                f"found={counts.get('found', 0)} failed={counts.get('failed', 0)}"
            )
            job.stats = {**(job.stats or {}), **counts}
            job.finished_at = datetime.now(UTC)
            session.add(job)
            session.commit()
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("scheduled publication check failed: %s", e)
            job.status = JobStatus.failed.value
            job.message = f"{type(e).__name__}: {e}"[:500]
            job.finished_at = datetime.now(UTC)
            session.add(job)
            session.commit()


def start_scheduler(cfg: CarrelYAML) -> AsyncIOScheduler | None:
    """Start the scheduler for any enabled cron jobs. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    sched = cfg.schedule
    jobs: list[tuple[str, callable, str, list]] = []  # type: ignore[type-arg]
    if sched.enabled:
        jobs.append(("daily_sync", _scheduled_sync, sched.sync_cron, [DEFAULT_LOOKBACK_HOURS]))
    if sched.remote_fill_enabled:
        jobs.append((
            "remote_fill",
            _scheduled_remote_fill,
            sched.remote_fill_cron,
            [DEFAULT_REMOTE_FILL_LIMIT],
        ))
    if sched.publication_check_enabled:
        jobs.append((
            "publication_check",
            _scheduled_publication_check,
            sched.publication_check_cron,
            [DEFAULT_PUBLICATION_CHECK_LIMIT],
        ))

    if not jobs:
        logger.info("scheduler disabled (no schedule jobs enabled)")
        return None

    scheduler = AsyncIOScheduler(daemon=True)
    for job_id, func, cron, args in jobs:
        scheduler.add_job(
            func,
            trigger=CronTrigger.from_crontab(cron),
            args=args,
            id=job_id,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info("scheduled job %s cron=%r", job_id, cron)
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("scheduler stopped")
