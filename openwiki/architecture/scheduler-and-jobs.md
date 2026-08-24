---
type: scheduler
title: Scheduler and background jobs
description: APScheduler-based cron registry, the Job table as the progress surface, and how API BackgroundTasks and scheduled runs share one execution model.
tags: [scheduler, apscheduler, jobs, background-tasks, cron]
---

# Scheduler and background jobs

All recurring work runs inside the FastAPI process via an APScheduler
`BackgroundScheduler` (`carrel/scheduler.py`). There is no separate worker
process or queue; the `jobs` table is the system-of-record for both scheduled
runs and one-off API-triggered jobs, and the frontend polls it to render
progress.

## Declarative job registry

`JOB_SPECS` is a tuple of frozen `JobSpec` dataclasses — the single source of
truth for both the scheduler and the `GET /schedule` status panel. Each spec
declares:

- `id`, `label`, `description` (UI-facing blurb),
- `kind` (the `JobKind` written to the jobs table for the last run),
- `func` (the body, run on a scheduler thread or the `run_job_now` thread),
- `cron_attr` and `enabled_attr` (attribute names on `ScheduleConfig`),
- `args`,
- `requires` (a gating label surfaced in the UI, e.g. `"remote_ssh"`).

The shipped specs are:

| ID | Cron config | Body | What it does |
|---|---|---|---|
| `daily_sync` | `schedule.sync_cron` (default `0 8 * * *`), `schedule.enabled` | `_scheduled_sync` | Calls `pipeline.runner.run_sync` with `DEFAULT_LOOKBACK_HOURS=26`. One Job row, stats include `new_discovered`, `updated`, `cross_id_dedup`, etc. |
| `remote_fill` | `schedule.remote_fill_cron` (`0 9 * * *`), `schedule.remote_fill_enabled` | `_scheduled_remote_fill` | No-op unless `remote_downloader.is_configured()`. Calls `publication_check.fill_closed_papers` for up to 50 closed papers. |
| `publication_check` | `schedule.publication_check_cron` (`0 10 * * 1`), `schedule.publication_check_enabled` | `_scheduled_publication_check` | Calls `publication_check.check_pending` for up to 50 old arXiv papers. |
| `wiki_compile` | `schedule.wiki_compile_cron` (`17 11 * * *`), `schedule.wiki_compile_enabled` | `_scheduled_wiki_compile` | Calls `wiki.scholar_compile.compile_scholars_pending` only. **Note:** this scheduled job compiles scholar pages only; the `POST /wiki/compile` API endpoint runs a four-stage pipeline (paper_extract → scholar → concept → question) — see [../wiki/compilers.md](../wiki/compilers.md). |

Cron strings are standard 5-field user crontab, parsed via APScheduler's
`CronTrigger.from_crontab`.

## Lifecycle

- `start_scheduler(cfg)` instantiates `BackgroundScheduler(timezone=UTC)`,
  iterates `JOB_SPECS`, and adds an APScheduler job for every spec whose
  `enabled_attr` is true. It stores the resulting APScheduler job id in a
  module dict keyed by `JobSpec.id` so `restart_scheduler` can diff it.
- `restart_scheduler(cfg)` is called by `PATCH /schedule` after the YAML is
  rewritten: it removes jobs that are no longer enabled, adds newly enabled
  ones, and replaces existing jobs whose cron changed.
- `stop_scheduler()` shuts the scheduler down on FastAPI lifespan shutdown.
- Each scheduled body opens its own `Session(engine)` (scheduler threads
  cannot reuse the request-scoped session), writes a `Job(status=running,
  started_at=now)`, calls the pipeline, and flips the row to `done` or
  `failed` with a short human `message` and full stats.

## Manual "Run now"

`POST /schedule/{job_id}/run` calls `run_job_now(spec_id)`, which:

1. Checks the in-process `_in_flight` set under `_in_flight_lock`; a second
   concurrent click while the same spec is already running returns
   `409 Conflict` ("already running"). This complements APScheduler's own
   `max_instances=1` (which only governs scheduled-vs-scheduled collisions,
   because `run_job_now` submits work to a fresh thread outside APScheduler).
2. Spawns a daemon thread that pops its id from `_in_flight` in a `finally`
   block, so a crash does not wedge the "Run now" button.

## API-triggered batch jobs

Endpoints like `POST /process`, `/embed`, `/summarize`, `/topics`,
`/authors-backfill`, `/papers/extract`, `/paper-dedup/run`,
`/scholar-dedup/run`, `/papers/{id}/check-publication`,
`/papers/{id}/refresh-citations`, and `/wiki/compile` all follow the same
pattern:

1. Insert one `Job(kind=..., status=queued, ...)` per target (or one Job for
   the whole batch for `/wiki/compile`, `/sync`, and dedup runs).
2. `session.flush()` to populate primary keys, commit, and refresh.
3. If the request says `background=true` (the default for most), schedule a
   FastAPI `BackgroundTasks` target that opens a new `Session` against the app
   engine and drives the pipeline; otherwise run inline and return the
   terminal Job rows.
4. The pipeline receives an `on_progress` callback that updates
   `job.stats.stage` / `job.stats.detail` and commits, so the UI's poll of
   `GET /sync/jobs/{id}` reflects live progress.

`POST /sync` follows the same shape but always creates a single Job row for
the whole fetch/upsert pass (see [../ingestion/sync.md](../ingestion/sync.md)).

## Job table contract

`Job` rows are append-only history. `stats` is arbitrary JSON, but the
frontends and the scheduled-job panel rely on these stable keys:

- `stage` — short machine token (`queued`, `download`, `parse`, `summarize`,
  `embed`, ...).
- `detail` — human text shown under the job title.
- `paper_id`, `paper_title` — for per-paper jobs, so the UI can link to the
  paper.
- Per-run counters such as `new_discovered`, `updated`, `cross_id_dedup`,
  `compiled`, `failed`, `candidates`, `parsed`, `limit`.

There is no automatic reaping of old Job rows; they remain for the Sync
Status page and are listed newest-first.

## Orphan reset

On every startup the lifespan runs
`UPDATE jobs SET status='failed', finished_at=now, message='Interrupted by
server restart' WHERE status IN ('queued','running')` (`carrel/main.py:91-97`).
Without this a uvicorn `--reload` or crash would leave jobs the UI polls
forever. Manual "Run now" also sets its own Job row to `failed` on
unhandled exceptions.

## Focused tests

- `tests/test_runner.py` exercises `run_sync` end-to-end including the
  per-source error isolation.
- `tests/test_process_api.py` covers Job creation, the `on_progress`
  callback's stage writes, and inline vs background execution.
- `tests/test_summarize_api.py`, `tests/test_topics_api.py`,
  `tests/test_paper_dedup_api.py` cover the per-domain Job wiring.
- `tests/test_wiki_api.py` covers the `/wiki/compile` batch Job.

## Validation

```bash
.venv/bin/python -m pytest tests/test_runner.py tests/test_process_api.py -q
```

## Evidence

- Scheduler implementation: `carrel/scheduler.py`.
- Schedule API: `carrel/api/schedule.py`.
- Schedule config writer: `carrel/config_store.py` (see
  [configuration.md](configuration.md)).
- Job model and kinds: `carrel/models.py:59-97, 601-618`.
- Sync status UI: `frontend/src/pages/SyncStatus.tsx`,
  `frontend/src/components/ScheduledJobsCard.tsx`,
  `frontend/src/components/TaskList.tsx`.
