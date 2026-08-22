"""APScheduler wrapper for the daily sync (M6).

Single-process, in-process scheduler. Reads `schedule.enabled` and
`schedule.sync_cron` from YAML. The cron expression is standard 5-field
user crontab format (minute hour dom month dow), parsed via APScheduler's
`from_crontab`.

The scheduler also exposes a small status/inspection API used by the
/schedule endpoint and the UI's scheduler panel.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select

from carrel.config import CarrelYAML
from carrel.db import get_app_engine
from carrel.models import Job, JobKind, JobStatus
from carrel.pipeline.runner import run_sync

logger = logging.getLogger("carrel.scheduler")

# Daily job: cover a bit more than 24h so a delayed/skipped run still sweeps.
DEFAULT_LOOKBACK_HOURS = 26

# Default batch sizes for the periodic sweeps.
DEFAULT_REMOTE_FILL_LIMIT = 50
DEFAULT_PUBLICATION_CHECK_LIMIT = 50
DEFAULT_WIKI_COMPILE_LIMIT = 20

_scheduler: BackgroundScheduler | None = None

# Job ids currently being executed by run_job_now, so a double-click on
# "Run now" doesn't fire two concurrent passes. APScheduler's own
# max_instances=1 already guards scheduled-vs-scheduled, but our manual
# thread bypasses that.
_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Job bodies
# ---------------------------------------------------------------------------


def _scheduled_sync(lookback_hours: int, *, manual: bool = False) -> None:
    """Body of the cron job — opens its own DB session (runs in a worker thread)."""
    from carrel.main import app_config  # noqa: PLC0415

    prefix = "manual" if manual else "scheduled"
    engine = get_app_engine()
    with Session(engine) as session:
        job = Job(
            kind=JobKind.sync.value,
            status=JobStatus.running.value,
            message=f"{prefix} (lookback_hours={lookback_hours})",
            started_at=datetime.now(UTC),
            stats={"lookback_hours": lookback_hours, "sources": None, "scheduled": not manual},
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        try:
            run_sync(session, app_config, lookback_hours=lookback_hours, job=job)
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("%s sync failed: %s", prefix, e)
            job.status = JobStatus.failed.value
            job.message = f"{type(e).__name__}: {e}"[:500]
            job.finished_at = datetime.now(UTC)
            session.add(job)
            session.commit()


def _scheduled_remote_fill(limit: int, *, manual: bool = False) -> None:
    """Try to download PDFs for closed papers via the institutional host."""
    from carrel.main import app_config  # noqa: PLC0415
    from carrel.pipeline.publication_check import fill_closed_papers
    from carrel.sources import remote_downloader

    prefix = "manual" if manual else "scheduled"
    if not remote_downloader.is_configured():
        logger.info("%s remote_fill skipped: institutional SSH not configured", prefix)
        return

    engine = get_app_engine()
    with Session(engine) as session:
        job = Job(
            kind=JobKind.remote_fill.value,
            status=JobStatus.running.value,
            message=f"{prefix} remote fill",
            started_at=datetime.now(UTC),
            stats={"limit": limit, "scheduled": not manual},
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
            logger.exception("%s remote fill failed: %s", prefix, e)
            job.status = JobStatus.failed.value
            job.message = f"{type(e).__name__}: {e}"[:500]
            job.finished_at = datetime.now(UTC)
            session.add(job)
            session.commit()


def _scheduled_publication_check(limit: int, *, manual: bool = False) -> None:
    """Check old arXiv papers for a published journal version."""
    from carrel.main import app_config  # noqa: PLC0415
    from carrel.pipeline.publication_check import check_pending

    prefix = "manual" if manual else "scheduled"
    engine = get_app_engine()
    with Session(engine) as session:
        job = Job(
            kind=JobKind.publication_check.value,
            status=JobStatus.running.value,
            message=f"{prefix} publication check",
            started_at=datetime.now(UTC),
            stats={"limit": limit, "scheduled": not manual},
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
            logger.exception("%s publication check failed: %s", prefix, e)
            job.status = JobStatus.failed.value
            job.message = f"{type(e).__name__}: {e}"[:500]
            job.finished_at = datetime.now(UTC)
            session.add(job)
            session.commit()


def _scheduled_wiki_compile(limit: int, *, manual: bool = False) -> None:
    """Compile stale scholar wiki pages (M8a)."""
    from carrel.main import app_config  # noqa: PLC0415
    from carrel.pipeline.wiki.scholar_compile import compile_scholars_pending

    prefix = "manual" if manual else "scheduled"
    engine = get_app_engine()
    with Session(engine) as session:
        job = Job(
            kind=JobKind.wiki_compile.value,
            status=JobStatus.running.value,
            message=f"{prefix} wiki compile",
            started_at=datetime.now(UTC),
            stats={"limit": limit, "scheduled": not manual},
        )
        session.add(job)
        session.commit()

        def _progress(p: dict[str, Any]) -> None:
            job.stats = {**(job.stats or {}), **p}
            idx, total, name = p.get("index"), p.get("total"), p.get("name", "")
            detail = p.get("detail", "")
            if idx and total:
                job.message = f"[{idx}/{total}] {name} — {detail}" if detail else f"[{idx}/{total}] {name}"
            elif detail:
                job.message = detail
            session.add(job)
            session.commit()

        try:
            counts = compile_scholars_pending(
                session, app_config, limit=limit, on_progress=_progress
            )
            job.status = JobStatus.done.value
            job.message = (
                f"compiled={counts.get('compiled', 0)} failed={counts.get('failed', 0)}"
            )
            job.stats = {**(job.stats or {}), **counts}
            job.finished_at = datetime.now(UTC)
            session.add(job)
            session.commit()
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("%s wiki compile failed: %s", prefix, e)
            job.status = JobStatus.failed.value
            job.message = f"{type(e).__name__}: {e}"[:500]
            job.finished_at = datetime.now(UTC)
            session.add(job)
            session.commit()


# ---------------------------------------------------------------------------
# Declarative job registry — single source of truth for both start_scheduler
# and the /schedule status endpoint.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobSpec:
    id: str
    label: str
    description: str  # human-readable "what does this task do?" blurb
    kind: str  # JobKind.value; used to look up last run in the jobs table
    func: Callable[..., Any]
    cron_attr: str  # attribute on ScheduleConfig holding the cron string
    enabled_attr: str  # attribute on ScheduleConfig holding the on/off bool
    args: tuple[Any, ...] = ()
    requires: str | None = None  # gating dep label surfaced in the UI


JOB_SPECS: tuple[JobSpec, ...] = (
    JobSpec(
        id="daily_sync",
        label="Metadata sync",
        description=(
            "Fetch new papers from every enabled subscription (keywords, authors, "
            "venues, arXiv categories) across OpenAlex, Semantic Scholar and arXiv, "
            "merge/deduplicate against the library, and upsert new matches into the "
            "inbox. Also backfills missing Semantic Scholar reference lists on "
            "library papers, and refreshes citation counts for a rolling batch of "
            "the stalest in-library papers each run. "
            "Cadence: controlled by the first cron field (minute) onwards — "
            "e.g. '0 8 * * *' = every day at 08:00 server time, "
            "'*/30 * * * *' = every 30 minutes."
        ),
        kind=JobKind.sync.value,
        func=_scheduled_sync,
        cron_attr="sync_cron",
        enabled_attr="enabled",
        args=(DEFAULT_LOOKBACK_HOURS,),
    ),
    JobSpec(
        id="remote_fill",
        label="Institutional PDF fill",
        description=(
            "Find papers in the library that have no open-access PDF, then SSH into "
            "the configured institutional host and try to download each PDF from "
            "the campus network via the scansci-pdf CLI. Successfully fetched PDFs "
            "are parsed by MinerU and attached to the paper. "
            "Cadence: e.g. '0 9 * * *' = every day at 09:00 server time, "
            "'0 9 * * 1-5' = weekdays at 09:00."
        ),
        kind=JobKind.remote_fill.value,
        func=_scheduled_remote_fill,
        cron_attr="remote_fill_cron",
        enabled_attr="remote_fill_enabled",
        args=(DEFAULT_REMOTE_FILL_LIMIT,),
        requires="remote_ssh",
    ),
    JobSpec(
        id="publication_check",
        label="arXiv → journal check",
        description=(
            "Look at older arXiv preprints in the library and check whether a "
            "peer-reviewed journal version has since been published. When a journal "
            "DOI is found, record it and, if a PDF is available, download that "
            "version alongside the arXiv one. "
            "Cadence: e.g. '0 10 * * 1' = every Monday at 10:00 server time, "
            "'0 10 1 * *' = the 1st of each month at 10:00."
        ),
        kind=JobKind.publication_check.value,
        func=_scheduled_publication_check,
        cron_attr="publication_check_cron",
        enabled_attr="publication_check_enabled",
        args=(DEFAULT_PUBLICATION_CHECK_LIMIT,),
    ),
    JobSpec(
        id="wiki_compile",
        label="Compile wiki",
        description=(
            "Compile scholar wiki pages from in-library paper metadata and "
            "abstracts. Each scholar gets an interlinked, source-cited Markdown "
            "page; pages whose authors have new papers are recompiled. User "
            "notes in the protected section are preserved. "
            "Cadence: e.g. '17 11 * * *' = every day at 11:17 server time."
        ),
        kind=JobKind.wiki_compile.value,
        func=_scheduled_wiki_compile,
        cron_attr="wiki_compile_cron",
        enabled_attr="wiki_compile_enabled",
        args=(DEFAULT_WIKI_COMPILE_LIMIT,),
    ),
)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _build_scheduler(cfg: CarrelYAML) -> BackgroundScheduler | None:
    """Build (but do not persist) a scheduler for every enabled job in cfg."""
    jobs: list[tuple[JobSpec, str]] = []
    for spec in JOB_SPECS:
        enabled = bool(getattr(cfg.schedule, spec.enabled_attr, False))
        if not enabled:
            continue
        cron = str(getattr(cfg.schedule, spec.cron_attr, "")).strip()
        if not cron:
            continue
        jobs.append((spec, cron))

    if not jobs:
        return None

    scheduler = BackgroundScheduler(daemon=True)
    for spec, cron in jobs:
        scheduler.add_job(
            spec.func,
            trigger=CronTrigger.from_crontab(cron),
            args=list(spec.args),
            id=spec.id,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        logger.info("scheduled job %s cron=%r", spec.id, cron)
    return scheduler


def start_scheduler(cfg: CarrelYAML) -> BackgroundScheduler | None:
    """Start the scheduler for any enabled cron jobs. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = _build_scheduler(cfg)
    if scheduler is None:
        logger.info("scheduler disabled (no schedule jobs enabled)")
        return None

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


def restart_scheduler(cfg: CarrelYAML) -> BackgroundScheduler | None:
    """Stop any running scheduler and start a fresh one from cfg.

    Called after the schedule section of config.yaml is rewritten by the
    PATCH /schedule endpoint. Idempotent: if nothing is enabled, stops any
    existing scheduler and returns None.
    """
    stop_scheduler()
    return start_scheduler(cfg)


def get_job_spec(job_id: str) -> JobSpec | None:
    for spec in JOB_SPECS:
        if spec.id == job_id:
            return spec
    return None


def run_job_now(spec: JobSpec) -> tuple[bool, str]:
    """Trigger one job body in a background thread; returns (dispatched, message).

    The job bodies open their own SQLModel session and write a ``Job`` row,
    so all we need to do here is fire them off and let the existing
    /sync/jobs polling surface the run. If a hard prerequisite (e.g. the
    institutional SSH config) is missing, refuse to dispatch and tell the
    caller why — that way the UI doesn't look like the button silently
    no-op'd.
    """
    if spec.requires == "remote_ssh":
        from carrel.sources import remote_downloader  # noqa: PLC0415

        if not remote_downloader.is_configured():
            return False, (
                "Institutional SSH is not configured "
                "(REMOTE_SSH_ENABLED and the REMOTE_* vars in .env)."
            )

    with _in_flight_lock:
        if spec.id in _in_flight:
            return False, f"{spec.label} is already running"
        _in_flight.add(spec.id)

    def _runner() -> None:
        try:
            spec.func(*spec.args, manual=True)
        finally:
            with _in_flight_lock:
                _in_flight.discard(spec.id)

    t = threading.Thread(
        target=_runner,
        name=f"manual-{spec.id}",
        daemon=True,
    )
    t.start()
    return True, f"{spec.label} triggered"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _last_run_for(session: Session, kind: str) -> Job | None:
    """Most recent Job row for a given kind (scheduled or manual)."""
    stmt = (
        select(Job)
        .where(Job.kind == kind)
        .order_by(Job.id.desc())
        .limit(1)
    )
    return session.exec(stmt).first()


def _requirement_satisfied(spec: JobSpec, cfg: CarrelYAML) -> bool:
    if spec.requires is None:
        return True
    if spec.requires == "remote_ssh":
        from carrel.sources import remote_downloader  # noqa: PLC0415

        return remote_downloader.is_configured()
    return True


def get_status(cfg: CarrelYAML, session: Session) -> dict[str, Any]:
    """Build the status payload for GET /schedule."""
    aps_jobs = {j.id: j for j in (_scheduler.get_jobs() if _scheduler else [])}

    out_jobs: list[dict[str, Any]] = []
    for spec in JOB_SPECS:
        enabled = bool(getattr(cfg.schedule, spec.enabled_attr, False))
        cron = str(getattr(cfg.schedule, spec.cron_attr, "") or "")
        aps_job = aps_jobs.get(spec.id)
        last = _last_run_for(session, spec.kind)

        next_run = getattr(aps_job, "next_run_time", None) if aps_job else None
        # APScheduler datetimes are tz-aware (UTC). Normalize to UTC so JSON
        # serialisation is stable regardless of the server's local tz.
        if next_run is not None and next_run.tzinfo is not None:
            next_run = next_run.astimezone(UTC)

        out_jobs.append({
            "id": spec.id,
            "label": spec.label,
            "description": spec.description,
            "enabled": enabled,
            "cron": cron,
            "running": aps_job is not None,
            "next_run_at": next_run,
            "last_status": last.status if last else None,
            "last_started_at": last.started_at if last else None,
            "last_finished_at": last.finished_at if last else None,
            "last_message": last.message if last else None,
            "last_stats": last.stats if last else None,
            "requires": spec.requires,
            "requirement_satisfied": _requirement_satisfied(spec, cfg),
        })

    # Master "scheduler is actually running" — true iff at least one APScheduler
    # job is registered (which is what `enabled` and its sub-switches resolve to).
    master_running = _scheduler is not None and bool(aps_jobs)

    return {
        "enabled": master_running,
        "jobs": out_jobs,
    }
