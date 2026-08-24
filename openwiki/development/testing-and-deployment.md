---
type: devops
title: Development, testing, and deployment
description: How to install, run, test, lint, and operate Carrel locally — Makefile targets, Docker Compose services, the SQLite test path, and the optional MinerU service.
tags: [devops, makefile, docker, testing, mineru, uvicorn]
---

# Development, testing, and deployment

Carrel is a single-user, self-hosted app. The backend runs under
uvicorn, the frontend under Vite in dev or as static files in
production, Postgres+pgvector runs via Docker Compose, and the MinerU
PDF parser is an optional external HTTP service.

## Prerequisites

- Docker Desktop (Windows/macOS) or Docker Engine on Linux (for
  Postgres).
- Python ≥ 3.11.
- Node.js ≥ 20.
- [`uv`](https://docs.astral.sh/uv/) preferred; the Makefile falls
  back to `pip`.

## Quick start

```bash
cp .env.example .env                       # edit secrets
mkdir -p data && cp config.example.yaml data/config.yaml

make up                                    # Postgres + pgvector on :5432
make install-backend                       # uv sync (or pip install -e .[dev])
make backend                               # uvicorn carrel.main:app on :8787
make install-frontend
make frontend                              # vite on :5173
```

Open http://127.0.0.1:5173. The backend creates tables on first boot
(`init_db` — see
[../backend/database.md](../backend/database.md)).

## MinerU (optional, for PDF → Markdown)

MinerU has no CPU Docker image (the official image is NVIDIA-GPU
only), so on CPU machines — including Apple Silicon — it is installed
into a dedicated venv and run natively:

```bash
make mineru-install     # creates .venv-mineru, installs mineru[core] + models
make mineru-up          # starts mineru-api on http://127.0.0.1:8000
make mineru-down
```

On Linux + NVIDIA GPU, build the official image and run it under the
`mineru` Compose profile:

```bash
make mineru-build-gpu
docker compose --profile mineru up -d
```

Carrel talks to MinerU over HTTP only (see
[../ingestion/pdf-processing.md](../ingestion/pdf-processing.md)), so
MinerU's AGPL-3.0 license does not propagate to Carrel's code.

## Makefile targets

| Target | Purpose |
|---|---|
| `make install` | Install backend (`uv sync`) and frontend (`npm install`) |
| `make up` / `make down` | Start/stop Postgres via Docker Compose |
| `make psql` | Open `psql` against the carrel DB |
| `make backend` | Run uvicorn with `--reload` on `127.0.0.1:8787` |
| `make frontend` | Run Vite dev server on `:5173` |
| `make start` / `stop` / `restart` | Detached dev-server control via `ops` skill; restart waits for health |
| `make status` | Show which dev servers are listening |
| `make doctor` / `heal` | Ops read-only health check / safe auto-fix (proxy bypass) |
| `make logs` | Tail Docker Compose logs |
| `make mineru-install` / `up` / `down` | Native MinerU install/start/stop (CPU) |
| `make mineru-build-gpu` | Build `mineru:latest` from the official Dockerfile |
| `make migrate-paper-dedup` | One-shot: scan library and auto-merge strong-anchor duplicates |

## Docker Compose

`docker-compose.yml` defines:

- `postgres` (`pgvector/pgvector:pg16`) on port `${POSTGRES_PORT:-5432}`,
  database `carrel`, user `carrel`, password `${POSTGRES_PASSWORD:-carrel_dev}`,
  with a named volume `postgres_data` and a `pg_isready` healthcheck.
- `mineru` (profile `mineru`) — only started with
  `docker compose --profile mineru up`. Requires a locally built
  `mineru:latest` image; GPU passthrough is commented out by default.

## Configuration

- YAML: `data/config.yaml` (path-relative; copy from
  `config.example.yaml`). All blocks and defaults are documented on
<!-- openwiki: broken internal link [configuration.md] file "configuration.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  [configuration.md](configuration.md).
- Secrets / connection strings: `.env` (copy from `.env.example`).
  The schedule can be edited at runtime through
  `PATCH /schedule`, which atomically rewrites the `schedule:` block
  of the YAML file via `carrel/config_store.py`.

## Storage layout

Under `cfg.storage.root` (default `./data`):

```
data/
  papers/<safe-slug>/
    paper.pdf              # active PDF (or arxiv.pdf / journal.pdf variants)
    paper.md               # MinerU markdown
    images/                # MinerU-extracted figures
  attachments/
  wiki/
    concepts/<slug>.md
    scholars/<slug>.md
    questions/<slug>.md
  config.yaml
```

The FastAPI app mounts this root at `/storage`
(`app.mount("/storage", StaticFiles(...))` in
`carrel/main.py`), and the frontend Vite proxy forwards `/storage`
to it. The directory is created on startup if it doesn't exist.

## Testing

All tests run against SQLite — no Docker required. Vector columns
fall back to JSON on SQLite (`VectorType = Vector(2048).with_variant(JSON(), "sqlite")`
in `carrel/models.py`); API tests boot the real FastAPI app with
`get_session_dep` overridden to an in-memory engine
(`tests/conftest.py`).

```bash
.venv/bin/python -m pytest                 # whole suite
.venv/bin/ruff check carrel/ tests/        # lint
.venv/bin/python -m pytest tests/test_paper_dedup.py -q   # one file
```

The README records the test count (65 at the time of M6); the suite
has since grown with dedup, wiki, and scholar tests. There are no
frontend tests at the time of writing; the frontend is type-checked
with `npm run lint` (`tsc --noEmit`).

## One-shot scripts in `scripts/`

- `migrate_paper_dedup.py` — scan the library and auto-merge
  strong-anchor duplicates (also wired through `make migrate-paper-dedup`).
- `cleanup_duplicate_wiki.py` — retire duplicate wiki pages via the
  same identity-reconciliation code that runs at startup.
- `seed_demo_paper.py` — insert a sample paper for local UI
  development.

## Ops health checks

- `GET /health` returns `{status, version, db, mineru, remote}` —
  `db` is the SQLAlchemy dialect name, `mineru` reflects the MinerU
  `/health` probe, `remote` is true when institutional SSH
  downloading is configured.
- `make doctor` / `make heal` wrap the `.claude/skills/ops/scripts/`
  checks that the Carrel author uses for local detached-server
  operation; they are not required to run the app.

## Evidence

- `Makefile`, `docker-compose.yml`, `pyproject.toml`,
  `frontend/package.json`.
- `.env.example`, `config.example.yaml`.
- `tests/conftest.py`, `tests/test_api.py`.
- `scripts/*.py`.
- Runtime bootstrap:
  [../backend/app-lifecycle.md](../backend/app-lifecycle.md),
  [../architecture/configuration.md](../architecture/configuration.md).
