---
type: frontend_overview
title: Frontend overview
description: Vite + React + TypeScript + Tailwind SPA — routing, API client and dev proxy, shared components, Markdown rendering with KaTeX/citations/wikilinks, and the page-to-API map.
tags: [frontend, react, vite, tailwind, markdown]
---

# Frontend overview

The frontend is a single-page app built with Vite + React 18 +
TypeScript + Tailwind CSS. It talks to the FastAPI backend over a
proxied `/api` prefix and renders parsed papers, hybrid/semantic
search results, the LLM-compiled wiki, and per-paper RAG chat.

## Stack

- **Build/dev**: Vite 5 with `@vitejs/plugin-react`. TypeScript
  project references (`tsc -b`).
- **Routing**: `react-router-dom` v6, routes declared in
  `frontend/src/App.tsx`.
- **Styling**: Tailwind CSS 3 with a shadcn-style UI kit under
  `frontend/src/components/ui/`.
- **Markdown**: `react-markdown` with `remark-gfm`, `remark-math`,
  `rehype-katex`, `rehype-raw`, plus three custom rehype plugins
  (citations, math, wikilinks).
- **Chat**: `@assistant-ui/react` for the chat transcript UI
  (lazy-loaded on the paper detail page).
- **Icons**: `lucide-react`.

## Dev server and proxy

`frontend/vite.config.ts` runs the dev server on `0.0.0.0:5173` and
proxies two prefixes to the backend at `http://127.0.0.1:8787`:

- `/api` → backend, with the `/api` prefix stripped (so a call to
  `/api/papers` reaches the backend's `/papers`).
- `/storage` → backend static mount (parsed paper images and PDFs).

All client code therefore uses relative paths like `/api/papers` (see
`API_BASE = "/api"` in `frontend/src/api/client.ts`). The production
build is static files; the same relative prefix is expected to be
served by a reverse proxy in front of FastAPI.

## API client — `frontend/src/api/client.ts`

A single hand-written module (no codegen) that:

- Exposes typed interfaces mirroring `carrel/schemas.py`
  (`PaperSummary`, `PaperDetail`, `ScholarSummary`, `WikiPageSummary`,
  `Job`, `SearchResultItem`, `SemanticSearchResult`, etc.).
- Wraps `fetch` in a `request<T>` helper that injects
  `Content-Type: application/json`, throws `APIError` on non-2xx, and
  treats 204 as `undefined`.
- Groups functions by backend domain (`getHealth`, `listPapers`,
  `getPaper`, `importPaper`, `discardPaper`, `searchPapers`,
  `searchSemantic`, `importPaper` from search results,
  `triggerSync`, `processPaper`, `embedPaper`, `summarizePaper`,
  `triggerTopics`, `compileWiki`, `recompileWikiPage`,
  `listScholars`, `getScholarWorks`, `listSubscriptions`,
  `setFavorite`, `saveNotes`, `addPaperTag`/`removePaperTag`,
  `chat` streaming helper, citation/refresh endpoints, dedup
  suggestions/merge/reject, schedule GET/PATCH, etc.).

It is the canonical place to inspect when an endpoint is added or
renamed — every page imports from it.

## Routing map

| Route | Page | Primary purpose |
|---|---|---|
| `/` | `pages/Search.tsx` | Hybrid search (combined local + external) |
| `/today` | `pages/Home.tsx` | Today card feed: health, library, inbox, subscriptions, sync/process/embed/summarize buttons, top journals |
| `/library` | `pages/Library.tsx` | Library list with filters (sort, q, favorite, tag, topic, status) and the paper-dedup panel |
| `/topics` | `pages/Topics.tsx` | Topics browse |
| `/scholars` | `pages/Scholars.tsx` | Aggregated author list with dedup suggestions |
| `/scholars/:key` | `pages/ScholarDetail.tsx` | Scholar profile, in-library papers, compiled wiki page, paged OpenAlex works |
| `/wiki` | `pages/WikiIndex.tsx` | Wiki landing / kind index |
| `/wiki/:kind` | `pages/WikiPageList.tsx` | List pages of a kind (concept/scholar/question) with filters |
| `/wiki/:kind/:slug` | `pages/WikiPageDetail.tsx` | Rendered wiki page with backlinks/sources |
| `/subscriptions` | `pages/Subscriptions.tsx` | Subscription CRUD + top-journal quick add |
| `/sync` | `pages/SyncStatus.tsx` | Job history and scheduled-job controls |
| `/papers/:id` | `pages/PaperDetail.tsx` | Paper reader, processing actions, citations, references, notes, tags, chat |

## Shared components

- `components/MarkdownReader.tsx` — central Markdown renderer. Plugs
  in `remark-gfm`, `remark-math`, `rehype-katex`, and the custom
  rehype plugins below; resolves MinerU's relative image links
  against `/storage/<md-dir>/...`; turns internal `/...` links into
  react-router `<Link>`s; styles MinerU's inline-HTML tables.
- `components/rehypeCitations.ts` — converts MinerU citation markers
  into anchor links to `#ref-n` reference entries.
- `components/rehypeRawMath.ts` — protects raw math that arrived
  outside standard delimiters.
- `components/rehypeWikiLinks.ts` — converts the wiki's
  `[[Label]](../kind/slug.md)` dual links into react-router navigation.
- `components/PaperChat.tsx` — RAG chat UI (lazy loaded). Consumes
  the SSE stream from `POST /papers/{id}/chat`, renders sources,
  persists the transcript via
  `GET/PUT /papers/{id}/chat/messages`.
- `components/NotesCard.tsx`, `components/CitationsCard.tsx`,
  `components/ReferencesCard.tsx`, `components/CitationRowActions.tsx`
  — paper detail panels.
- `components/PaperDedupPanel.tsx` — Duplicates panel on the Library
  page, calls `/paper-dedup/suggestions`, `/run`, `/merge`, `/reject`.
- `components/ScheduledJobsCard.tsx` — sync status page scheduler
  panel (status, run-now, enable/cron PATCH).
- `components/TaskList.tsx`, `components/StatusDot.tsx`,
  `components/OaBadge.tsx`, `components/PaperList.tsx`,
  `components/TopicSidebar.tsx`, `components/TopJournalSection.ts`.
- `components/ui/` — shadcn-style primitives (button, card, tooltip,
  etc.).

## Utilities — `frontend/src/lib`

- `topicColor.ts` — deterministic Tailwind color class per topic
  name, shared by list/sidebar/detail views.
- `useDebouncedCallback.ts` — debounce hook used for q/filter inputs.
- `utils.ts` — `cn(...)` class-name merger.

## State and data fetching

There is no global data library (React Query/Redux). Each page owns
its `useState`/`useEffect` fetches against the typed client, and
long-running jobs are polled via `getJob(id)` until `status` is
`done`/`failed` (e.g. `TERMINAL = new Set(["done","failed"])` in
`PaperDetail.tsx`). The scheduler panel reads/writes schedule config
through `/schedule`.

## Build and check

```bash
cd frontend
npm run lint     # tsc --noEmit
npm run build    # tsc -b && vite build
npm run dev      # vite dev server on :5173
```

## Evidence

- Routes: `frontend/src/App.tsx`.
- API client: `frontend/src/api/client.ts`.
- Vite config/proxy: `frontend/vite.config.ts`.
- Dependencies: `frontend/package.json`.
- Markdown rendering: `frontend/src/components/MarkdownReader.tsx`,
  `components/rehypeCitations.ts`, `components/rehypeRawMath.ts`,
  `components/rehypeWikiLinks.ts`.
- Pages map to the backend endpoints listed on
  [../backend/api-reference.md](../backend/api-reference.md); paper
  actions chain to [../ingestion/pdf-processing.md](../ingestion/pdf-processing.md),
  [../enrichment/summarization.md](../enrichment/summarization.md),
  and [../enrichment/embeddings.md](../enrichment/embeddings.md);
  search and chat to [../backend/search-and-chat.md](../backend/search-and-chat.md).
