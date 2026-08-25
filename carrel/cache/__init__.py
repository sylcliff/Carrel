"""Persistent read-through cache for OpenAlex Work lookups.

This package owns the helpers that back the three OpenAlex waste spots the
PR targets:

  * A — :class:`carrel.api.scholars.list_scholar_works` serves
    :class:`AuthorWorksCache` rows instead of paged OpenAlex cursors.
  * B — :func:`lookup_work_by_arxiv_id` walks a 3-layer read-through
    (in-library paper.raw_meta → WorkByArxivId → OpenAlex live + write-back)
    so sync / publication_check / search share one cache row.
  * D — :func:`lookup_work_by_oa_id` extends the same idea to the
    ``Works()[oa_id]`` path used by the import resolver.

Long-running fetches live in :mod:`carrel.pipeline.scholar_works_sync`
(separate module per the existing pipeline-vs-cache convention).
"""
