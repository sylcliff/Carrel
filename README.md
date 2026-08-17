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

This is **M1 (skeleton)** of the [plan](./PLAN.md). The app boots against a
local Postgres; the rest of the pipeline lands in M2–M6.

| Milestone | Status |
|---|---|
| M1 骨架 (this commit) | ✅ Postgres+pgvector, FastAPI, React+Vite+TS+Tailwind, health & paper list pages |
| M2 抓取 + 库页 | ⏳ arXiv + OpenAlex fetchers, normalize, dedup |
| M3 PDF + MinerU | ⏳ |
| M4 LLM 摘要 | ⏳ |
| M5 检索 | ⏳ |
| M6 定时 + 订阅 UI | ⏳ |
| M7 打磨 | ⏳ |

---

## Quick start (M1)

Prereqs:
- Docker Desktop (Windows/macOS) or Docker Engine on Linux
- Python ≥ 3.11
- Node.js ≥ 20

```bash
# 1. secrets (edit values, do not commit)
cp .env.example .env
cp config.example.yaml data/config.yaml   # path-relative config

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

Open <http://127.0.0.1:5173> — you should see the "Today" page with a "Sync
now" button (it just queues a job in M1; actual fetching ships in M2).

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
