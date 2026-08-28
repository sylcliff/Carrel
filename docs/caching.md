# Caching in Carrel

Carrel uses three independent cache layers, each shippable on its own. They cooperate — when a write invalidates L2, L1 ETag naturally changes (the source `updated_at` shifted), and the React Query client refetches on the next stale window. The combined effect is fast page navigation with strict write consistency.

| Layer | Where | TTL | Granularity | Invalidation |
|---|---|---|---|---|
| **L1** HTTP `Cache-Control` + `ETag` | FastAPI response headers | 30s list / 60–600s detail | Per-endpoint + filter fingerprint | `If-None-Match` 304 + write-side hook |
| **L2** In-process LRU | Python process (`OrderedDict` + lock) | Until invalidated | Per route + key params, fan-out via tags | Event-driven via `_invalidation` helpers |
| **L3** React Query | Browser (`@tanstack/react-query`) | 30s `staleTime`, 5min `gcTime` | Per `queryKey` tuple | `invalidateQueries({queryKey})` after mutations |

SSE streams (`POST /api/papers/{id}/chat`, `POST /api/wiki/chat`) and search endpoints (`GET /api/search/*`, `GET /api/search/semantic/*`) are excluded from all three layers. Both set `Cache-Control: no-store` defensively.

## L1 — HTTP ETag + 304

Read endpoints emit `ETag: W/"…"` and `Cache-Control: private, max-age=N, stale-while-revalidate=M` on every response. The browser re-validates by sending `If-None-Match`; on a match the server returns `304 Not Modified` with an empty body.

Two ETag recipes:

- **Detail**: `W/"{id}-{updated_at.isoformat()}"`. Tied to the row's `updated_at` so any write that bumps it produces a new tag automatically.
- **List**: `W/"{count}:{ids_digest}:{max_updated_at}"`. Embeds the row id set so collisions require both the max timestamp **and** the row set to be bit-identical — which is exactly the 304 case we want.

The frontend attaches `If-None-Match` automatically via `requestCached<T>()` in `frontend/src/api/client.ts` (see L3 below).

```bash
# First call — 200, gets an ETag.
curl -i http://localhost:8765/api/papers/abc | head -20

# Second call with the prior ETag — 304, empty body.
curl -i -H 'If-None-Match: W/"abc-2026-08-28T10:11:12.345678+00:00"' \
     http://localhost:8765/api/papers/abc | head -5
```

## L2 — In-process LRU

`carrel/api/_app_cache.py` defines a thread-safe LRU keyed by `"route:key_params#tags"`. Read endpoints opt in via the `@cached(route, key_params=..., tags=..., offset_invariant=...)` decorator. The decorator:

1. Computes a stable key from the route + the named `key_params`.
2. Returns the cached value on hit; runs the wrapped function and stores its result on miss.
3. Records `last_status = "HIT"` or `"MISS"` for `/health?debug=1` and curl probes.

### Fan-out invalidation

Every entry declares its `tags` at write time. `cache.invalidate_tags("papers_list", ...)` drops every entry that declared any of those tags. Per-id invalidation uses `cache.invalidate_exact("paper:{id}")` instead.

### Write-side hooks

All write endpoints go through helpers in `carrel/api/_invalidation.py`. The map is documented in `~/.claude/plans/cosmic-marinating-locket.md` (Invalidation map). The short version:

| Write endpoint | Helper |
|---|---|
| `POST /api/papers/{id}/favorite`, `/notes`, `/tags`, `/import`, `/discard` | `invalidate_paper_mutated(id, mutate=...)` |
| `DELETE /api/papers/{id}` | `invalidate_paper_mutated(id, mutate={"deleted"})` |
| `POST /api/import/bulk` (in `_drive_batch` final commit) | `invalidate_bulk_import_done()` |
| `POST /api/refresh-citations` (on job success) | `invalidate_citations_refreshed(id)` |
| `POST /api/topics` (on job success) | `invalidate_topics_recomputed()` |
| `POST /api/wiki/compile` / `recompile` (on done) | `invalidate_wiki_recompiled()` |
| `PATCH /api/settings` (after YAML write) | `invalidate_settings_changed()` |

The `tests/test_cache_invalidation.py` suite monkey-patches `cache.invalidate_tags` and asserts that every annotated write calls an `_invalidation` helper. Add the same assertion to a new write endpoint's test.

### Memory bound

`maxsize=512` by default. With ~50 KB per entry, worst case is ~25 MB. The LRU evicts on overflow — older filter combinations are the first to go.

## L3 — React Query (frontend)

`@tanstack/react-query@^5.62.0` lives in the browser. Defaults: `staleTime: 30_000`, `gcTime: 5 * 60_000`, `refetchOnWindowFocus: false`, `retry: 1`. Per-query overrides set `staleTime: Infinity` for resources that are slow to change and have explicit invalidation on mutation: paper markdown, settings, topics, tags.

The frontend has two pieces of L3-specific plumbing:

- **`requestCached<T>(path)`** in `frontend/src/api/client.ts`. Module-scope `Map<string, string>` of ETags and `Map<string, unknown>` of bodies. On 304 we resolve from the cache without re-parsing JSON. On 200 we refresh both maps. A 304 with no prior body throws `APIError(500)` so the next call refills the registry.
- **`useApiQuery` / `useApiMutation`** in `frontend/src/lib/useApiQuery.ts`. The `useApiMutation` wrapper enforces the optimistic-update contract: `onOptimistic` runs before `mutate()`, `onRollback` restores prior state on error, and `invalidate: [...]` fires after success to mark peers stale.

### Query key conventions

Centralized in `frontend/src/lib/queryKeys.ts`. First segment = resource, second = id (string) where applicable, sub-resources nest. `invalidateQueries({queryKey: ["paper", id]})` cascades to all sub-resources via React Query's prefix matching.

```ts
queryKeys = {
  papersRoot:    () => ["papers"] as const,
  papersList:    (f: PaperFilters) => ["papers", f] as const,
  paper:         (id: string) => ["paper", id] as const,
  paperMarkdown: (id: string) => ["paper", id, "markdown"] as const,   // Infinity
  paperCitations:  (id, p) => ["paper", id, "citations", p] as const,
  paperReferences: (id, sort) => ["paper", id, "references", sort] as const,
  paperTags:     (id: string) => ["paper", id, "tags"] as const,
  topics:        () => ["topics"] as const,        // Infinity
  tags:          () => ["tags"] as const,          // Infinity
  settings:      () => ["settings"] as const,      // Infinity
};
```

### Optimistic updates

Pattern: capture `queryClient = useQueryClient()` at the top of the component. In `useApiMutation`, `onOptimistic` writes via `qc.setQueryData`, `onRollback` restores it. For provisional→confirmed swaps (e.g. optimistic tag add then replace with server-saved id), use `mutationOptions.onSuccess` — the wrapper's own `onSuccess` only receives `(output, input)`, not the `queryClient`.

## Telemetry

`GET /api/health?debug=1` returns the L2 cache stats alongside the standard probe:

```json
{
  "status": "ok",
  "version": "…",
  "db": "up",
  "mineru": "http://localhost:8765",
  "remote": true,
  "cache": {
    "size": 18,
    "maxsize": 512,
    "tags": 8,
    "hits": 142,
    "misses": 23,
    "invalidations": 4,
    "last_status": "HIT"
  }
}
```

Interpretation:

- **`size` close to `maxsize`**: LRU is full. Most filter combinations are unique — confirm the filter key is stable. If `size` stays near `maxsize` but the working set is small, the `@cached` decorator is being called with too many distinct `key_params`.
- **`hits / (hits + misses) < 0.5`**: invalidations are too aggressive (over-fanning out) or the TTL is too short relative to the access pattern. Check the write-side hooks in `carrel/api/_invalidation.py` for `invalidate_tags(...)` calls that drop a tag they shouldn't.
- **`invalidations` growing fast while `size` is low**: a write path is fanning out broadly. `invalidate_paper_mutated("*", ...)` from `DELETE /api/tags/{id}` legitimately hits the whole library, but if `DELETE /api/papers/{id}` does the same, you have a bug.

The default `?debug=0` (and absent query string) is a cheap probe that doesn't read from the cache. The frontend boot path uses that.

## Debugging stale-cache reports

The fastest way to confirm L2 is the problem (not L1 ETag or the React Query cache):

1. `curl -i http://localhost:8765/api/papers/abc` — note the ETag.
2. Toggle a field in the DB (or via the UI) and watch the response: a new ETag confirms the L1 layer is alive.
3. `curl -i http://localhost:8765/api/papers/abc` again with the new ETag — 304 means L1 short-circuited. Re-issue without `If-None-Match` to force a full read; the L2 will flip `last_status` between `MISS` and `HIT`.
4. `curl http://localhost:8765/api/health?debug=1` to see the aggregate.

If the browser shows stale data but the curl probe shows fresh, the L3 cache is the culprit. Open DevTools → React Query devtools (or the Network tab) and inspect the query key + `dataUpdatedAt` timestamp.

## Risk register (carried from the plan)

| Risk | Status | Mitigation |
|---|---|---|
| R1 — Stale-by-30s on Library list after a write | Mitigated | Write-side invalidation cascades to React Query's `["papers"]` prefix. |
| R2 — ETag collisions on equal `updated_at` | Mitigated | List ETag embeds the row id set; collisions require bit-identical timestamp + row set. |
| R3 — Flash of old data after a PATCH | Mitigated | Optimistic `setQueryData` *before* the mutation; rollback on error. |
| R4 — LRU memory growth | Mitigated | `maxsize=512` ≈ 25 MB worst case. |
| R5 — Stale data on bulk import | Mitigated | `invalidate_bulk_import_done()` in `_drive_batch`; React Query refetches on poll completion. |
| R6 — L1 + L2 double-caching | Intentional | Compound benefit. Relies on every write going through the invalidation hook — see AGENTS.md. |
| R7 — SSE endpoints | Mitigated | Middleware hard-sets `Cache-Control: no-store` on `text/event-stream` responses. |
| R8 — `/search/*` always-fresh | Mitigated | Explicit `Cache-Control: no-store` on every search handler. |
| R9 — Cache stampede on cold LRU | Accepted | Not a concern at single-user scale. A future `asyncio.Lock`-per-key stampede protection is one line in the decorator. |
| R10 — `paper.updated_at` bumped by chat PUT | Accepted | New ETag is correct; the row really was touched. |

## What this design does NOT do

- No distributed cache (Redis/Memcached). Single-process assumption holds for Carrel.
- No cross-tab invalidation beyond React Query's `invalidateQueries`. Multi-tab freshness for a write in tab A relies on tab B's `staleTime` window. A `BroadcastChannel` plumbing layer is a follow-up.
- No HTTP/2 push, server-sent cache invalidation, WebSocket cache buster.
- Wiki SSE chat and paper chat are already excluded.

## Operator cheat sheet

| Symptom | First check | Then check |
|---|---|---|
| Page is stale after a write | `curl /api/health?debug=1` — confirm `invalidations` jumped | Confirm the write endpoint calls an `_invalidation` helper (see `_invalidation.py`) |
| LRU full | `cache.size` vs `cache.maxsize` | Filter key instability; check `key_params` in the `@cached` decorator |
| L1 not short-circuiting | DevTools Network → `If-None-Match` header absent | `requestCached` in the client (L3 layer) vs `request` |
| SSE response cached | `Cache-Control` header on the response | SSE middleware in `carrel/main.py` |
| Search results stale across users (single-user app, but in case) | `/api/search/*` `Cache-Control` | `search.py` — should set `no-store` explicitly |
