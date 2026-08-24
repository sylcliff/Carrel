---
type: configuration
title: Configuration and secrets
description: How Carrel loads configuration from data/config.yaml and .env, every CarrelYAML block and EnvSettings key, and how PATCH /schedule writes config back atomically.
tags: [configuration, yaml, env, pydantic-settings, scheduler]
---

# Configuration and secrets

Carrel merges two layers at startup (`carrel/config.py`):

1. **`data/config.yaml`** — non-secret runtime configuration, parsed as
   `CarrelYAML` (Pydantic model). Copy `config.example.yaml` to
   `data/config.yaml` to get started.
2. **Environment / `.env`** — secrets and connection strings, parsed as
   `EnvSettings` (pydantic-settings). Copy `.env.example` to `.env`.

`load_settings(yaml_path)` returns `(CarrelYAML, EnvSettings)` and applies a
fixed set of env-over-YAML overrides for connection strings, API keys, model
ids, and the HTTP/CORS bind. There is no deep-merge — env wins for the
specific fields listed below.

The active config path is hard-coded as `CONFIG_PATH = Path("data/config.yaml")`
in `carrel/main.py:50`; run the backend from the repository root so the
relative path resolves.

## YAML blocks

| Block | Fields (defaults in parentheses) | Consumed by |
|---|---|---|
| `storage` | `root` (`./data`), `papers_subdir` (`papers`), `attachments_subdir` (`attachments`), `wiki_subdir` (`wiki`); helpers `paper_dir()`, `attachments_dir()`, `wiki_dir()`, `wiki_kind_dir(kind)` | `main._bootstrap_config`, `pipeline.process`, `pipeline.wiki.*`, static mount |
| `http` | `host` (`127.0.0.1`), `port` (`8787`) | uvicorn invocation in `Makefile`; env override only (the app itself binds via uvicorn CLI) |
| `cors` | `origins` (`["http://127.0.0.1:5173"]`) | `main.create_app` CORS middleware (Vite dev origins are always appended) |
| `openalex` | `mailto`, `api_key`, `request_timeout_seconds` (30), `max_retries` (3), `search_enabled` (True), `search_per_page` (20) | `sources.openalex_client.configure` |
| `arxiv` | `request_timeout_seconds` (30), `max_retries` (3), `max_results_per_query` (200), `delay_between_requests_seconds` (3.0), `search_enabled`, `search_per_page` | `sources.arxiv.fetch_recent`, `pipeline.runner` |
| `semantic_scholar` | `base_url`, `api_key`, `request_timeout_seconds`, `max_retries`, `rate_limit_per_second` (auto 1.0 with key / 0.5 without), `delay_between_requests_seconds` (deprecated, ignored), `citations_limit` (500), `fetch_on_sync` (True), `references_backfill_batch` (50), `citations_refresh_batch` (25), `search_enabled`, `search_per_page` | `sources.semanticscholar_client.configure`, `pipeline.citations`, `api.search` |
| `download` | `request_timeout_seconds` (60), `max_bytes` (80 MiB), `user_agent` | `pipeline.process._step_download`, `sources.pdf_download.download_pdf_with_fallback` |
| `llm` | `summarize_provider/model`, `fallback_provider/model`, `temperature` (0.2), `request_timeout_seconds` (60), `max_input_chars` (12 000), `chat_model`, `chat_fallback_model`, `chat_temperature` (0.3), `rag_top_k` (6), `chat_history_limit` (6), `chat_fulltext_chars` (24 000), plus `paper_dedup_judge_model`, `paper_dedup_judge_fallback`, `paper_dedup_judge_prompt_version` (1), `paper_dedup_judge_max_calls_per_run` (200) | `pipeline.summarize`, `pipeline.topics`, `pipeline.paper_extract`, `pipeline.wiki.*`, `api.chat`, `pipeline.paper_dedup_judge` |
| `embeddings` | `provider` (`volcengine`), `model` (`volcengine/doubao-embedding-large-text-240915`), `dim` (2048), `request_timeout_seconds` (60), `batch_size` (50) | `embeddings.embed_texts`, `pipeline.embed`, wiki page embeddings |
| `mineru` | `base_url` (`http://127.0.0.1:8000`), `request_timeout_seconds` (900), `backend` (`pipeline`), `parse_method` (`auto`), `lang_list` (`["en"]`), `formula_enable`, `table_enable` | `sources.mineru_client.parse_pdf`, `pipeline.process._step_parse` |
| `chunking` | `target_tokens` (900), `overlap_tokens` (150), `min_tokens` (200) | `pipeline.embed.embed_paper` |
| `schedule` | `enabled` (False), `sync_cron` (`0 8 * * *`), `remote_fill_enabled`/`_cron`, `publication_check_enabled`/`_cron`, `wiki_compile_enabled`/`_cron` | `scheduler.start_scheduler`, `api.schedule` |
| `subscriptions` | top-level list of `{kind, value, label?}` seeded into the `subscriptions` table at config load if not already present | `pipeline.runner.list_enabled_subscriptions` (DB is authoritative at runtime; this is seed data) |

## Environment / `.env` keys

`EnvSettings` (pydantic-settings, reads `.env` automatically):

- **Database**: `DATABASE_URL`
  (`postgresql+psycopg://carrel:carrel_dev@127.0.0.1:5432/carrel`). Tests
  override this to a temp SQLite file in `tests/conftest.py`.
- **LLM / embedding API keys**: `DEEPSEEK_API_KEY`, `VOLCANO_API_KEY`
  (the code spells this `volcano_api_key`; set `VOLCANO_API_KEY` in the
  environment), `OPENAI_API_KEY` (optional, via litellm prefix map).
- **Source keys / contact**: `OPENALEX_API_KEY`, `OPENALEX_MAILTO`,
  `S2_API_KEY`.
- **Model overrides**: `SUMMARIZE_MODEL`, `FALLBACK_MODEL`,
  `EMBEDDING_MODEL`, `EMBEDDING_DIM`.
- **Service endpoints**: `MINERU_BASE_URL`.
- **Bind / CORS overrides**: `CARREL_HOST`, `CARREL_PORT`,
  `CARREL_CORS_ORIGINS` (comma-separated). These mutate `cfg.http.*` /
  `cfg.cors.origins` in `load_settings` (lines 304–309).
- **Institutional SSH download (all optional)**: `REMOTE_SSH_ENABLED`,
  `REMOTE_SSH_HOST`, `REMOTE_SSH_PORT` (22), `REMOTE_SSH_USER`,
  `REMOTE_SSH_KEY_PATH`, `REMOTE_SSH_KNOWN_HOSTS_PATH`,
  `REMOTE_SSH_CONNECT_TIMEOUT` (25), `REMOTE_WORK_DIR`,
  `REMOTE_COMMAND_TEMPLATE` (defaults to a `scansci-pdf get` invocation),
  `REMOTE_DL_TIMEOUT` (240), `REMOTE_RETRIES` (3), plus
  `REMOTE_JOURNAL_MIN_AGE_DAYS` (180) and
  `REMOTE_JOURNAL_CHECK_THROTTLE_DAYS` (30) for the arXiv→journal detector.

`.env.example` is the documented template; do not commit a real `.env`.

## Env-over-YAML precedence

`load_settings` applies these overrides in order after both layers load
(`carrel/config.py:282-309`). There is no YAML counterpart for
`database_url` — the database URL is consumed directly from
`EnvSettings.database_url` by `db.init_app_engine`.

1. `DATABASE_URL` is used directly by `db.make_engine` (no YAML field).
2. `OPENALEX_MAILTO` fills `cfg.openalex.mailto` only when the YAML has none.
3. `OPENALEX_API_KEY`, `S2_API_KEY` overwrite their YAML slots when present.
4. `SUMMARIZE_MODEL`, `FALLBACK_MODEL`, `EMBEDDING_MODEL`,
   `EMBEDDING_DIM`, `MINERU_BASE_URL` overwrite their YAML slots.
5. `CARREL_HOST`, `CARREL_PORT` overwrite `cfg.http.host` / `cfg.http.port`.
6. `CARREL_CORS_ORIGINS` (comma-separated) **replaces** `cfg.cors.origins`.

Secrets never live in YAML; the embedding/LLM wrappers read their keys
directly from `os.environ` via the provider-prefix map in
`carrel/embeddings.py:_KEY_ENV`.

## Writing config back from the UI

`PATCH /schedule` (`carrel/api/schedule.py:patch_schedule`) updates only the
`schedule:` block. The handler maps exceptions to HTTP status codes:

- `ValueError` (unknown field, wrong type, bad cron) → **400** with the
  error detail.
- `OSError` (could not write the file) → **500**.

On success it mutates the **live** `app_config.schedule` object in place
(other routers holding a reference to `app_config` see the new values
without a restart) and calls `sched_mod.restart_scheduler(cfg)`.

Persistence is delegated to
`carrel/config_store.py:update_schedule(path, updates)`:

1. Load the raw YAML dict with `load_raw` (missing file → `{}`).
2. For each update, only accept keys in `FIELD_KEYS` and type-check
   against the declared Python type; unknown keys are ignored.
3. Re-validate the merged `schedule:` block against `ScheduleConfig`
   (Pydantic enforces types/defaults).
4. **Re-parse every enabled cron string** with
   `apscheduler.triggers.cron.CronTrigger.from_crontab(...)` so a typo
   (e.g. `99 * * * *`) fails the request instead of taking the scheduler
   down on restart. A bad cron raises `ValueError` and the API returns
   400 with the offending field name.
5. `save_raw` writes via `tempfile.mkstemp` + `os.replace` for an atomic
   swap — a crash mid-write cannot leave a truncated `config.yaml`.

## Storage layout

`<cfg.storage.root>/` (default `./data/`):

```
data/
  config.yaml
  papers/
    <safe-slug>/              # id with :/ replaced by _ (pdf_download.safe_paper_dir)
      paper.pdf               # active PDF (always this name)
      paper.md                # MinerU markdown
      images/                 # MinerU images
      arxiv.pdf, journal.pdf  # variants kept by publication_check
  attachments/
  wiki/
    concepts/<slug>.md
    scholars/<aid-or-name--slug>.md
    questions/<slug>.md
```

The StaticFiles mount at `/storage` (`main.create_app`) serves this entire
tree; the frontend rewrites MinerU's relative `images/...` links against
`/storage/<md-dir>/...` in `components/MarkdownReader.tsx`.

## Validation

- Load both layers and print the merged model:

  ```bash
  .venv/bin/python -c "from carrel.config import load_settings; c,e=load_settings('data/config.yaml'); print(c.model_dump_json(indent=2))"
  ```

- Validate a YAML edit without starting the server:

  ```bash
  .venv/bin/python -c "from carrel.config import load_yaml; print(load_yaml('data/config.yaml'))"
  ```

- Config/schedule behavior is exercised end-to-end by the FastAPI TestClient
  in `tests/test_api.py` and the scheduler tests under
  `tests/test_runner.py` (schedule config gating).

## Evidence

- All settings models and the loader: `carrel/config.py`.
- Atomic schedule writer: `carrel/config_store.py`, `carrel/api/schedule.py`.
- Boot-time path creation and CORS origin build: `carrel/main.py`.
- Templates: `config.example.yaml`, `.env.example`.
- Makefile targets that bind host/port: `Makefile` (`backend`,
  `install-backend`).
