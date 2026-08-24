# Files

- [PDF download and MinerU parsing](pdf-processing.md) - The process_paper state machine that downloads an OA PDF (with multi-URL fallback and institutional SSH), validates %PDF magic, parses to Markdown via MinerU, and chains a non-fatal LLM summary.
- [arXiv-to-journal publication check](publication-check.md) - Detects when an arXiv preprint has been formally published, records the journal DOI, keeps the journal PDF alongside the arXiv PDF, promotes and re-parses it when the institutional downloader can fetch it.
- [Metadata source clients](sources.md) - The arXiv Atom, OpenAlex (pyalex), and Semantic Scholar httpx clients, plus the PaperRecord normalizer and the cross-source merge/RRF layer used by search.
- [Sync pipeline](sync.md) - The run_sync orchestration that partitions subscriptions, fetches candidates from arXiv + OpenAlex, merges by canonical id, cross-id dedups against existing rows, upserts into the inbox, and runs bounded citation backfill/refresh sweeps.
