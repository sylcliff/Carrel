PYTHON ?= python
POETRY ?= uv
NPM    ?= npm

.PHONY: help install backend frontend up down psql mineru-build mineru-up logs

help:
	@echo "Carrel — common targets:"
	@echo "  make install      - install backend (uv) and frontend (npm) dependencies"
	@echo "  make up           - start Postgres+pgvector via docker compose"
	@echo "  make down         - stop all docker compose services"
	@echo "  make mineru-build - build the mineru:latest image (one-time, requires NVIDIA GPU optional)"
	@echo "  make mineru-up    - start MinerU API on :8000 (profile: mineru)"
	@echo "  make backend      - run FastAPI dev server (uvicorn --reload)"
	@echo "  make frontend     - run Vite dev server"
	@echo "  make psql         - open psql against the carrel DB"

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

mineru-build:
	@echo "Building mineru:latest from the official Dockerfile..."
	@mkdir -p .references/mineru-docker && cd .references/mineru-docker && \
	  curl -fsSL https://gcore.jsdelivr.net/gh/opendatalab/MinerU@master/docker/global/Dockerfile -o Dockerfile && \
	  docker build -t mineru:latest -f Dockerfile .
	@echo "Image 'mineru:latest' built. Next: 'make mineru-up' to start the API service."

mineru-up:
	docker compose --profile mineru up -d
	@echo "MinerU API starting on :8000 (first run downloads model weights — may take a while)."

# --- Dev servers --------------------------------------------------------------

backend:
	$(POETRY) run uvicorn carrel.main:app --host 127.0.0.1 --port 8787 --reload || \
	  $(PYTHON) -m uvicorn carrel.main:app --host 127.0.0.1 --port 8787 --reload

frontend:
	cd frontend && $(NPM) run dev

logs:
	docker compose logs -f --tail=100
