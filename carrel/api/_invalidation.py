"""Cache invalidation hooks (Layer 2 + Layer 1 complement).

Every write endpoint that mutates a paper (or any cached collection) MUST
call one of the helpers in this module before returning. The plan's R6 risk
covers why: if a write path forgets to invalidate, the next read returns
stale data, and the user is confused.

The helpers are intentionally tiny — each maps to a semantic event. The
invalidation logic itself lives in :mod:`carrel.api._app_cache`.

Naming convention:

- ``invalidate_paper_mutated(id, mutate=...)`` — a single paper was changed.
  The ``mutate`` set tells the hook which *list* tags to fan out. Per-id
  invalidation is unconditional.
- ``invalidate_bulk_import_done()`` — the bulk-import job finished; every
  list view that depends on library size or per-row facets must refresh.
- ``invalidate_citations_refreshed(paper_id)`` — citations/references for
  a single paper were recomputed.
- ``invalidate_topics_recomputed()`` — the topic-classification job finished.
- ``invalidate_wiki_recompiled()`` — wiki compile/recompile/enrich finished.
- ``invalidate_settings_changed()`` — settings PATCH succeeded.
"""
from __future__ import annotations

from carrel.api._app_cache import get_cache


# The set of list-style tags that should be invalidated when *any* paper
# changes. The fan-out covers the Library page in every filter+sort
# combination because each combination is a separate cache entry.
_PAPER_LIST_TAGS = ("papers_list",)


def invalidate_paper_mutated(paper_id: str | None, *, mutate: set[str]) -> None:
    """Invalidate the cache for a single paper mutation.

    ``mutate`` should be a set describing what changed. Recognized members:

    - ``"favorite"`` — favorites flipped. Fan out to scholars list because
      scholars UI surfaces a per-author count of favorites.
    - ``"notes"`` — note text changed. Fan out to papers list because
      future features may show note snippets.
    - ``"tags"`` — per-paper tag add/remove. Fan out to the global tags
      list (counts change).
    - ``"inbox"`` — import/discard flipped ``in_library``. Fan out to
      topics too because inbox papers don't carry topics but their
      re-classification now runs.
    - ``"discarded"`` — soft delete. Same as ``"inbox"``.
    - ``"deleted"`` — hard delete. Superset of all the above.
    - ``"status"``, ``"markdown"``, ``"summary"``, ``"embeddings"``,
      ``"chat"`` — pipeline transitions. Currently no list impact, but
      we fire the per-id invalidation to keep the detail page fresh.
    - ``"citations"`` — citations were refreshed; redundant with the
      targeted :func:`invalidate_citations_refreshed` but harmless.

    When ``paper_id`` is None or ``"*"``, only the list tags are
    invalidated (no per-id exact drop, because we don't know the id —
    e.g. ``DELETE /tags/{tid}``).
    """
    cache = get_cache()
    # Per-id exact drop (always, when we know the id).
    if paper_id and paper_id != "*":
        cache.invalidate_exact(f"paper:{paper_id}")
        cache.invalidate_exact(f"paper:{paper_id}:markdown")
        cache.invalidate_exact(f"paper:{paper_id}:citations")
        cache.invalidate_exact(f"paper:{paper_id}:references")
        cache.invalidate_exact(f"paper:{paper_id}:tags")
    # List-style fan-out.
    tags = set(_PAPER_LIST_TAGS)
    if "tags" in mutate or "deleted" in mutate:
        tags.add("tags")
    if "inbox" in mutate or "discarded" in mutate or "deleted" in mutate:
        tags.add("topics")
    if "favorite" in mutate or "deleted" in mutate:
        tags.add("scholars_list")
    if tags:
        cache.invalidate_tags(*tags)


def invalidate_bulk_import_done() -> None:
    """Fan out invalidation to every list view after a bulk import completes.

    The fan-out covers the same tags as ``invalidate_paper_mutated`` for
    ``"inbox"`` mutations, plus the global topics + tags + scholars
    because counts and per-row facets may all have changed.
    """
    cache = get_cache()
    cache.invalidate_tags("papers_list", "topics", "tags", "scholars_list")


def invalidate_citations_refreshed(paper_id: str) -> None:
    """Drop the per-paper citations and references cache entries.

    We use exact-key invalidation, not the ``citations`` tag, because the
    tag would also evict citations entries for *other* papers (which are
    not stale). The per-id exact drop is the precise signal.
    """
    cache = get_cache()
    cache.invalidate_exact(f"paper:{paper_id}:citations")
    cache.invalidate_exact(f"paper:{paper_id}:references")


def invalidate_topics_recomputed() -> None:
    """Drop the global topics cache and any list view that depends on facets."""
    cache = get_cache()
    cache.invalidate_tags("topics", "papers_list")


def invalidate_wiki_recompiled() -> None:
    """Drop every wiki cache entry."""
    cache = get_cache()
    cache.invalidate_tags("wiki")


def invalidate_settings_changed() -> None:
    """Drop the global settings cache after a PATCH."""
    cache = get_cache()
    cache.invalidate_tags("settings")
