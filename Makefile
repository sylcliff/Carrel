PYTHON ?= python
POETRY ?= uv
NPM    ?= npm

.PHONY: help install backend frontend up down psql mineru-install mineru-up mineru-down logs \
        doctor heal start stop restart status

help:
	@echo "Carrel — common targets:"
	@echo "  make install         - install backend (uv) and frontend (npm) dependencies"
	@echo "  make up              - start Postgres+pgvector via docker compose"
	@echo "  make down            - stop all docker compose services"
	@echo "  make mineru-install  - one-time: pip-install MinerU (.venv-mineru) + pipeline models (CPU, Apple Silicon OK)"
	@echo "  make mineru-up       - start native mineru-api on :8000"
	@echo "  make mineru-down     - stop mineru-api"
	@echo "  make backend         - run FastAPI dev server in foreground (uvicorn --reload)"
	@echo "  make frontend        - run Vite dev server in foreground"
	@echo "  make start           - start backend+frontend detached (logs in /tmp/carrel-ops)"
	@echo "  make stop            - stop backend+frontend"
	@echo "  make restart         - restart backend+frontend, wait until healthy"
	@echo "  make status          - show which dev servers are listening"
	@echo "  make doctor          - run the ops health check (read-only)"
	@echo "  make heal            - auto-fix safe issues (proxy bypass)"
	@echo "  make psql            - open psql against the carrel DB"

# --- Dependencies -------------------------------------------------------------

install: install-backend install-frontend

install-backend:
	$(POETRY) sync 2>/dev/null || $(PYTHON) -m pip install -e ".[dev]"

install-frontend:
	cd frontend && $(NPM) install

# --- Docker compose -----------------------------------------------------------

up:
	docker compose up -d postgres
	@echo "Postgres up on :5432 (user=carrel password=carrel_dev)"

down:
	docker compose down

psql:
	docker compose exec postgres psql -U carrel -d carrel

# --- MinerU (optional, M3+) ---------------------------------------------------
# MinerU has no CPU Docker image (the official docker/global/Dockerfile is
# NVIDIA-GPU only). On CPU machines — including Apple Silicon — install it into
# a dedicated venv with pip and run mineru-api natively. Set MINERU_PY to use a
# specific interpreter; defaults to the local .venv-mineru.
MINERU_PY ?= .venv-mineru/bin/python

# One-time: create an isolated venv, install mineru[core], and fetch the
# pipeline (CPU) models only — no VLM weights.
mineru-install:
	@if [ ! -d .venv-mineru ]; then $(POETRY) venv .venv-mineru --python 3.12; fi
	$(POETRY) pip install --python .venv-mineru/bin/python -U "mineru[core]>=3.4.0"
	.venv-mineru/bin/mineru-models-download -s modelscope -m pipeline
	@echo "MinerU installed in .venv-mineru. Start it with 'make mineru-up'."

# Start the native mineru-api on :8000 using the already-downloaded models.
mineru-up:
	MINERU_MODEL_SOURCE=local nohup .venv-mineru/bin/mineru-api \
	  --host 127.0.0.1 --port 8000 > /tmp/carrel-mineru.log 2>&1 &
	@echo "mineru-api starting on http://127.0.0.1:8000 (log: /tmp/carrel-mineru.log)"

mineru-down:
	-lsof -ti tcp:8000 | xargs kill 2>/dev/null
	@echo "mineru-api stopped."

# GPU path (Linux + NVIDIA only). Not usable on Apple Silicon.
mineru-build-gpu:
	@echo "Building mineru:latest (NVIDIA GPU image) from the official Dockerfile..."
	@mkdir -p .references/mineru-docker && cd .references/mineru-docker && \
	  curl -fsSL https://gcore.jsdelivr.net/gh/opendatalab/MinerU@master/docker/global/Dockerfile -o Dockerfile && \
	  docker build -t mineru:latest -f Dockerfile .
	@echo "Image built. Start it with: docker compose --profile mineru up -d"

# --- Dev servers --------------------------------------------------------------

backend:
	$(POETRY) run uvicorn carrel.main:app --host 127.0.0.1 --port 8787 --reload || \
	  $(PYTHON) -m uvicorn carrel.main:app --host 127.0.0.1 --port 8787 --reload

frontend:
	cd frontend && $(NPM) run dev

logs:
	docker compose logs -f --tail=100

# --- Dev server control (detached) -------------------------------------------
# `backend`/`frontend` above run in the foreground for live logs. These targets
# delegate to the ops skill's scripts for detached start/stop/restart that waits
# on health endpoints, so "make restart" returns only when the app is reachable.
# The scripts live inside the skill (.claude/skills/ops/scripts/) and resolve
# their own paths, so these work from any cwd.

OPS := .claude/skills/ops/scripts

start:
	@$(OPS)/dev.sh start

stop:
	@$(OPS)/dev.sh stop

restart:
	@$(OPS)/dev.sh restart

status:
	@$(OPS)/dev.sh status

doctor:
	@$(OPS)/doctor.sh

heal:
	@$(OPS)/heal.sh
