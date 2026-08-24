---
type: backend_lifecycle
title: FastAPI application lifecycle
description: create_app, lifespan, config bootstrap, engine init, orphan-job reset, shared HTTP client configuration, router mounting order, and the /storage static mount.
tags: [fastapi, lifespan, startup, router, cors]
---

# FastAPI application lifecycle

`carrel/main.py` is the only composition root. It exposes a module-level
`app = create_app()` used by uvicorn (`carrel.main:app`) and by the FastAPI
TestClient. Two module globals, `app_config: CarrelYAML` and
`app_env: EnvSettings`, are populated by `lifespan` and imported lazily by
routers and pipelines that need live config (e.g. `api.chat._chat_model`,
`pipeline.wiki.scholar_compile`).

## Startup sequence

1. **`create_app()` (build time)**
   - Calls `_bootstrap_config()` *before* constructing the FastAPI instance so
     CORS can be configured from YAML even if lifespan has not yet run.
   - Builds `cors_origins` by de-duplicating the Vite dev origins
     (`http://127.0.0.1:5173`, `http://localhost:5173`) with
     `cfg.cors.origins`. A bootstrap failure falls back to the dev origins so
     a missing `config.yaml` does not break local development.
   - Creates the FastAPI app with `title="Carrel"`, `version=__version__`,
     the `lifespan` context manager, and permissive CORS
     (`allow_credentials=True`, `allow_methods=["*"]`,
     `allow_headers=["*"]` restricted to the configured origins).
   - Mounts all routers in the order below.
   - Mounts `/storage` as `StaticFiles(directory=cfg.storage.root.resolve())`
     last so it cannot shadow an API route. A mount failure is logged but
     does not prevent startup.

2. **`lifespan()` (run time)**
   - `_bootstrap_config()` again (reuses the same values; the design comment
     explicitly avoids a second reload so middleware, routers, and pipelines
     see identical config).
   - Creates storage directories: `cfg.storage.root`, `paper_dir()`,
     `attachments_dir()`, and `wiki_kind_dir(kind)` for `concept`,
     `scholar`, `question`.
   - `engine = init_app_engine(env)` (see [database.md](database.md)).
   - `init_db(engine)`: creates the pgvector extension, all tables, additive
     column migrations, backfills, wiki-identity reconciliation, and HNSW
     indexes.
   - Orphan-job reset: flips every `queued`/`running` Job left over from a
     previous process to `failed` with message
     `"Interrupted by server restart"`.
   - Configures the shared external HTTP clients:
     - `semanticscholar_client.configure(...)` with base URL, optional API
       key, timeout, retries, and RPS.
     - `openalex_client.configure(cfg)` (sets pyalex mailto/key, timeout,
       retry, and the capped-Retry-After requests session).
   - Assigns `app_config = cfg`, `app_env = env`, then
     `start_scheduler(cfg)`.
   - On shutdown, `stop_scheduler()` is called.

## Router mount order

`main.create_app` calls `app.include_router(...)` in this order
(`carrel/main.py:152-171`):

1. `health`
2. `papers`
3. `citations`
4. `annotations`
5. `subscriptions`
6. `schedule`
7. `sync`
8. `process`
9. `publication`
10. `summarize`
11. `topics`
12. `scholars`
13. `wiki`
14. `paper_extract`
15. `authors_backfill`
16. `scholar_dedup`
17. `paper_dedup`
18. `embed`
19. `search`
20. `chat`

Order matters for overlapping prefixes only in edge cases (e.g. `/papers/...`
routes defined across `papers`, `citations`, `annotations`, `chat`,
`publication`, `paper_extract`); FastAPI matches in registration order. The
`/storage` static mount is appended after every router so a file named like an
API path cannot shadow it.

## Late imports of live config

Several modules need `app_config` but cannot import it at module load (that
would create a circular import: `main` imports the routers, which would import
`main`). They resolve it inside the request/Job path, for example:

```python
def _chat_model() -> str:
    from carrel.main import app_config
    return app_config.llm.chat_model or app_config.llm.summarize_model
```

The same pattern is used by `api.schedule._get_app_config` (returns both
`app_config` and `CONFIG_PATH`), `api.chat._storage_root`, and the per-paper
pipelines invoked from background tasks. When writing a new pipeline that
needs config, prefer passing `cfg` down through function arguments from the
scheduler or API entry point (which already takes it from `app_config`); use
the late import only for leaf helpers.

## CORS and dev proxy

- The Vite dev server rewrites `/api/*` to `http://127.0.0.1:8787/*`
  (`frontend/vite.config.ts`); the backend does not see an `/api` prefix.
- `CARREL_CORS_ORIGINS` in `.env` overrides `cfg.cors.origins` (comma
  separated). Always include the origin of any non-localhost frontend you
  deploy; the Vite defaults are not automatically trusted in production.

## Versioning

`carrel/__init__.py` defines `__version__`; it surfaces in
`GET /health` and the FastAPI app metadata. Bump it when shipping a milestone
that affects the API or on-disk format.

## Focused tests

- `tests/test_api.py` boots the real app via `TestClient` against an in-memory
  SQLite engine (via the `client` fixture in `tests/conftest.py`), exercising
  the lifespan, dependency overrides, and router composition without Docker.
- `tests/conftest.py` shows how to override `get_session_dep` with an
  in-memory session for API tests. The scheduled `JOB_SPECS` are exercised
  indirectly through `tests/test_runner.py` (the `daily_sync` body),
  `tests/test_publication_check.py`, and `tests/test_wiki_api.py`; there is
  no direct APScheduler test because cron timing is a configuration concern.

## Run locally

```bash
.venv/bin/uvicorn carrel.main:app --host 127.0.0.1 --port 8787 --reload
# or
make backend
```

Smoke test the booted app:

```bash
curl -s http://127.0.0.1:8787/health
.venv/bin/python -m pytest tests/test_api.py -q
```

## Evidence

- App factory and lifespan: `carrel/main.py`.
- CORS config and env override: `carrel/main.py:122-150`,
  `carrel/config.py:304-309`.
- Storage bootstrap: `carrel/main.py:57-70`,
  `carrel/config.StorageConfig`.
- Scheduler start/stop: `carrel/scheduler.py` (see
  [architecture/scheduler-and-jobs.md](../architecture/scheduler-and-jobs.md)).
- App tests: `tests/test_api.py`, `tests/conftest.py`.
