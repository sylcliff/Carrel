<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional just-in-time context, not required startup reading.

- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->

## Caching — write-side rules (read before adding an endpoint)

Carrel has a 3-layer cache (L1 HTTP ETag, L2 in-process LRU, L3 React Query). The layers cooperate but only if **every write endpoint invalidates correctly**. Adding an endpoint without invalidation will silently serve stale data. See `docs/caching.md` for the full design.

### Backend (Python) checklist for a NEW write endpoint

- [ ] After the DB commit, call the appropriate helper in `carrel/api/_invalidation.py`:
  - `invalidate_paper_mutated(id, mutate={...})` for per-paper writes (favorite / notes / tags / status / import / discard).
  - `invalidate_bulk_import_done()` at the end of `_drive_batch` (or any bulk-import path).
  - `invalidate_citations_refreshed(id)` in the citations refresh job's success branch.
  - `invalidate_topics_recomputed()` in the topics job's success branch.
  - `invalidate_wiki_recompiled()` in the wiki compile/enrich success branch.
  - `invalidate_settings_changed()` after a YAML settings write.
  - Add a new helper + register it in `_invalidation.py` if none of the above fit.
- [ ] The `mutate={...}` set on `invalidate_paper_mutated` only controls which **list** tags get fanned out. The per-id `cache.invalidate_exact(f"paper:{id}")` always fires.
- [ ] Bump `paper.updated_at` on every write to a `Paper` row so the L1 ETag rotates (the model setter does this; if you write via raw SQL you must `UPDATE papers SET updated_at = now() ...`).
- [ ] Add a test in `tests/test_cache_invalidation.py` (or a new file if the write lives in a new module) that monkey-patches `cache.invalidate_tags` and asserts the right tag is invalidated after your endpoint runs.

### Backend (Python) checklist for a NEW read endpoint

- [ ] Decorate the read function with `@cached(route, key_params=..., tags=...)` from `carrel/api/_app_cache.py`.
  - `route` is a short identifier (e.g. `"paper"`, `"papers_list"`).
  - `key_params` is the tuple of argument names whose values feed the key.
  - `tags` lists every invalidation bucket this entry belongs to (e.g. `("paper", "papers_list")`).
  - `offset_invariant=False` only for endpoints where each offset page is rarely revisited; default is `True` and that's what list/detail endpoints want.
- [ ] Call `apply_etag_headers(response, etag, max_age=..., stale_while_revalidate=...)` from `carrel/api/_http_cache.py` on the response. Detail endpoints: `max_age=60, stale_while-revalidate=120`. List endpoints: `max_age=30, stale_while-revalidate=60`.
- [ ] Use `etag_for_updated_at(updated_at)` or `etag_for_list(...)` to build the ETag. Don't roll your own.
- [ ] If the endpoint serves a streaming response or anything that should never be cached (search, SSE, sync progress), set `Cache-Control: no-store` explicitly and **do not** apply the L2 decorator.

### Frontend (TypeScript) checklist for a NEW page or mutation

- [ ] Use `useApiQuery` from `frontend/src/lib/useApiQuery.ts` for any read that should benefit from `requestCached` (L1 304). Avoid raw `useEffect`+`fetch` for GETs that have a queryKey.
- [ ] Use `useApiQueryWithFn` for queries with a non-trivial queryFn (e.g. one that calls `listPapersPagedCached`).
- [ ] If the resource is slow to change and your mutation will invalidate it explicitly (settings, topics, tags, paper markdown), set `staleTime: Infinity` in the `useApiQuery` options.
- [ ] Use `useApiMutation` for any write that should be optimistic. Declare `invalidate: [...]` for every key the server-side write affects.
- [ ] Capture `queryClient = useQueryClient()` at the top of the component if you need it in `mutationOptions.onSuccess` (the wrapper's own `onSuccess` does not pass `queryClient`).
- [ ] Use the key factory in `frontend/src/lib/queryKeys.ts`. Never inline string arrays — the prefix-matching cascade (`["paper", id]` → `["paper", id, "markdown"]`, etc.) only works if the keys are built through the factory.
- [ ] If you migrate a page from `useEffect`+`useState` to React Query, delete the now-unused `useEffect` blocks. Local-only UI state (timers, expand/collapse flags, form input) stays in `useState` — that is not "server state."

### Telemetry

- [ ] For new backend code, you do not need to add anything to `/health?debug=1` — `AppCache.stats()` is automatic. If you add a new module with its own cache, expose its stats under `cache.<module>` in the `health.py` response.
- [ ] When debugging a stale-cache report: `curl /api/health?debug=1` first. Check `size`, `hits / (hits + misses)`, and the growth rate of `invalidations`. See `docs/caching.md` for the interpretation table.
