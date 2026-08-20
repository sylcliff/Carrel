# Carrel

> A self-hosted, single-user paper reading room.
> Take back control of your reading list — automatic ingest, full-text search,
> bilingual AI summaries, no cloud lock-in.

Carrel pulls new papers from arXiv (and OA copies on OpenAlex), parses them to
Markdown via [MinerU](https://github.com/opendatalab/MinerU), generates an
English + Chinese TL;DR per paper, and lets you read and search them all in
one place. Built as one user, one machine, one Postgres.

---

## Status

M1–M6 are done. The app boots against a local Postgres, pulls papers from
arXiv + OpenAlex (normalized/deduped onto an OpenAlex Work ID, arXiv ID
fallback), shows them in the library, downloads OA PDFs and parses them to
Markdown via a self-hosted MinerU service, generates bilingual LLM summaries
(chained after parse), and supports full-text hybrid search.

| Milestone | Status |
|---|---|
| M1 骨架 | ✅ Postgres+pgvector, FastAPI, React+Vite+TS+Tailwind, health & paper list pages |
| M2 抓取 + 库页 | ✅ arXiv + OpenAlex fetchers, normalize, dedup, subscription CRUD, sync jobs, library page |
| M3 PDF + MinerU | ✅ OA PDF download (content-validated), MinerU HTTP client, `pending→pdf_ready→parsed`, Markdown reader, `/process` jobs |
| M4 LLM 摘要 | ✅ bilingual TL;DR + Chinese summary + keywords via DeepSeek/Ark (litellm), chained after parse, `/summarize` jobs, fill-missing (preserves S2 tldr), non-fatal |
| M5 检索 | ✅ chunking + Ark embeddings + pgvector (`halfvec`), hybrid search + RRF, search page |
| M6 定时 + 订阅 UI | ✅ APScheduler cron sync + sync log page + subscription CRUD |
| M7 打磨 | 🟡 favorites/tags/notes, citations/references, failure retries done; manual PDF import pending |

---

## Quick start (M1)

Prereqs:
- Docker Desktop (Windows/macOS) or Docker Engine on Linux
- Python ≥ 3.11 (the Makefile uses [uv](https://docs.astral.sh/uv/); it falls
  back to `pip` if uv is absent)
- Node.js ≥ 20

```bash
# 1. secrets (edit values, do not commit)
cp .env.example .env
mkdir -p data && cp config.example.yaml data/config.yaml   # path-relative config

# 2. start Postgres + pgvector
make up

# 3. install Python deps (uv preferred, pip also works)
make install-backend

# 4. start backend (auto-creates tables on first run)
make backend          # http://127.0.0.1:8787/health  -> {"status":"ok",...}

# 5. install + start frontend in a second terminal
make install-frontend
make frontend         # http://127.0.0.1:5173
```

Open <http://127.0.0.1:5173>, add a subscription on the Subscriptions page
(a keyword or `cs.CL`), then hit **Sync now (72h)**. Fetched papers appear
in the library with status `pending`.

### Parsing PDFs to Markdown (M3)

M3 downloads an OA PDF and sends it to a self-hosted MinerU API.

**CPU machines (including Apple Silicon):** MinerU has no CPU Docker image (the
official Docker image is NVIDIA-GPU only), so install it natively into a
dedicated venv and run `mineru-api`:

```bash
make mineru-install   # one-time: pip install mineru[core] + pipeline models (~2.5 GB)
make mineru-up        # start MinerU on http://127.0.0.1:8000
```

**Linux + NVIDIA GPU:** use the official image instead: `make mineru-build-gpu`
then `docker compose --profile mineru up -d`.

Then on a paper's detail page click **Download & parse**, or use **Process
pending** on the Today page to parse the backlog. Papers move
`pending → pdf_ready → parsed`; failures are recorded on the paper and can be
retried. The parsed Markdown (with formulas, tables, and images) renders in
the detail page; the original PDF is kept on disk. MinerU options
(`backend`, `lang_list`, …) live under the `mineru:` block of `config.yaml`.

### Development

```bash
# Tests run fully on SQLite — no Docker required.
.venv/bin/python -m pytest          # 65 tests
.venv/bin/ruff check carrel/ tests/
```

---

## Project layout

```
carrel/                FastAPI backend (SQLModel + async scheduler)
frontend/              Vite + React + TS + Tailwind + shadcn-style UI
docker-compose.yml     Postgres 16 + pgvector (and optional MinerU)
config.example.yaml    User-facing config (paths, schedules, models)
.env.example           Secrets and connection strings
docs/architecture.md   Engineering blueprint (what to copy from where)
.references/           Read-only clones of upstream reference projects
```

See [`PLAN.md`](./PLAN.md) for the product scope and
[`docs/architecture.md`](./docs/architecture.md) for the technical skeleton and
"which file came from where" notes.

---

## License

MIT for our code. See [PLAN.md §10](./PLAN.md#10-参考项目已调研) for the upstream
projects and their licenses (paper-agent MIT, pyalex MIT, MinerU AGPL-3.0 — we
call it as a separate Docker process so AGPL does not extend to our code).
