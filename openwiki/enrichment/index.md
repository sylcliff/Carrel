# Files

- [Author A-ID backfill](authors-backfill.md) - Resolves missing OpenAlex Author IDs on in-library papers by exact DOI/arXiv lookup, repairing fragmented Scholar pages without fuzzy name matching.
- [Citation enrichment](citations.md) - Semantic Scholar-driven citation and reference enrichment for in-library papers, with OpenAlex cites-union fallback, bounded sync sweeps over stale and reference-less papers, and library-membership resolution in the citations API.
- [Chunking and embeddings](embeddings.md) - Heading-aware Markdown chunker and the embed_paper pipeline that chunks parsed papers, embeds each chunk via litellm, and writes pgvector Vector(2048) / halfvec(2048) rows for semantic search.
- [LLM summarization](summarization.md) - Bilingual TL;DR and Chinese summary + keyword generation for parsed papers, with fill-missing semantics, litellm-backed model/fallback selection, JSON-only prompt contract, and non-fatal failure handling.
- [Topic classification](topics.md) - LLM classifier that assigns 1-4 broad, reusable research topics to in-library papers from metadata only, growing a shared Topic vocabulary via many-to-many PaperTopic rows.
