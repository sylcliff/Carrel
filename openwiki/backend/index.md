# Files

- [HTTP API reference](api-reference.md) - Complete router table for every FastAPI endpoint, shared request/response patterns, background-job and progress-callback conventions, and links to the detailed domain pages.
- [FastAPI application lifecycle](app-lifecycle.md) - create_app, lifespan, config bootstrap, engine init, orphan-job reset, shared HTTP client configuration, router mounting order, and the /storage static mount.
- [Database engine, sessions, and bootstrap](database.md) - make_engine, pgvector registration, init_db with additive column migrations and HNSW indexes, wiki-identity reconciliation, the session dependency, and the SQLite test fallback.
- [Papers, library, and user annotations](papers-and-library.md) - The /papers API surface (list filters/sorts, inbox import/discard, hard delete with cleanup and disk-safety guard, markdown serving) and the favorites/notes/tags annotations router.
- [Scholars aggregation and API](scholars.md) - How authors are aggregated across in-library papers, the TTL-cached /scholars endpoints, OpenAlex profile lookup, the /works cursor pagination, and the compiled wiki-page join.
- [Search and RAG chat](search-and-chat.md) - Local SQL search, parallel multi-source external search (OpenAlex + Semantic Scholar + arXiv) with RRF merging and spell correction, pgvector semantic search, the /import resolution flow, and the per-paper SSE RAG chat.
