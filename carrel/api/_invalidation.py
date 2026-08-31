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


# What each ``mutate`` member fans out to. Membership is per-verb so a
# caller that flips both ``"tags"`` and ``"favorite"`` gets the union of
# the two fan-outs. ``"deleted"`` is a superset of the bookkeeping-y
# members; ``"inbox"`` and ``"discarded"`` both flip ``in_library`` —
# which (a) re-triggers the topic-classification UI and (b) changes the
# author set the /scholars aggregator sees — so they fan out to
# ``scholars_list`` as well.
_MUTATE_TO_TAGS: dict[str, frozenset[str]] = {
    "tags": frozenset({"tags"}),
    "inbox": frozenset({"topics", "scholars_list"}),
    "discarded": frozenset({"topics", "scholars_list"}),
    "favorite": frozenset({"scholars_list"}),
    "card": frozenset({"paper:card"}),
    "deleted": frozenset({"tags", "topics", "scholars_list"}),
}


# Verbs whose effect is visible on the Library list view (row visibility,
# row content, sort/filter outputs). Notes is included because the
# docstring flags it as forward-looking. ``status`` / ``markdown`` /
# ``summary`` / ``embeddings`` / ``chat`` / ``card`` / ``citations`` are
# explicitly excluded — they only touch per-paper sub-resources, so the
# list view is unaffected and the fan-out was just wasted churn.
_LIST_AFFECTING_VERBS = frozenset(
    {"tags", "inbox", "discarded", "favorite", "deleted", "notes"}
)


def invalidate_paper_mutated(paper_id: str | None, *, mutate: set[str]) -> None:
    """Invalidate the cache for a single paper mutation.

    ``mutate`` should be a set describing what changed. Recognized members:

    - ``"favorite"`` — favorites flipped. Fan out to scholars list because
      scholars UI surfaces a per-author count of favorites.
    - ``"notes"`` — note text changed. Fan out to papers list because
      future features may show note snippets.
    - ``"tags"`` — per-paper tag add/remove. Fan out to the global tags
      list (counts change) and the library list (row chips change).
    - ``"inbox"`` — import/discard flipped ``in_library``. Fan out to
      topics (inbox papers don't carry topics but their re-classification
      now runs) and to scholars_list (the /scholars aggregator keys off
      ``Paper.authors`` on in-library papers, so a new import adds rows).
    - ``"discarded"`` — soft delete. Same as ``"inbox"``.
    - ``"deleted"`` — hard delete. Superset of all the above.
    - ``"status"``, ``"markdown"``, ``"summary"``, ``"embeddings"``,
      ``"chat"`` — pipeline transitions. Currently no list impact, but
      we fire the per-id invalidation to keep the detail page fresh.
    - ``"citations"`` — citations were refreshed; redundant with the
      targeted :func:`invalidate_citations_refreshed` but harmless.
    - ``"card"`` — paper card (re-)extracted. Drops the per-paper card
      cache entry and the global card tag so a re-read rebuilds.

    When ``paper_id`` is None or ``"*"``, only the list tags are
    invalidated (no per-id exact drop, because we don't know the id —
    e.g. ``DELETE /tags/{tid}``).

    The per-id drop is a single ``invalidate_tags("paper_id:{id}")`` call:
    the ``@cached`` decorator auto-tags every per-paper entry with
    ``f"paper_id:{paper_id}"``, so a single tag match drops *every*
    sub-resource (paper, markdown, sections, references, tags, card) and
    every secondary-param variant (sort, offset) for the row. This is
    self-extending — new per-paper routes participate automatically as
    long as they put ``paper_id`` in ``key_params``.
    """
    cache = get_cache()
    # Per-id fan-out: drops every per-paper sub-resource for this id.
    if paper_id and paper_id != "*":
        cache.invalidate_tags(f"paper_id:{paper_id}")
    # Tag-based fan-out: union of the per-verb buckets in _MUTATE_TO_TAGS.
    # The papers_list tag fires only for verbs whose effect is visible
    # on the library list view; pipeline-only transitions (status,
    # markdown, summary, embeddings, chat, card, citations) skip it so
    # an idle page tab doesn't churn every list-shaped entry. See
    # _LIST_AFFECTING_VERBS for the full set.
    extra_tags: set[str] = set()
    for verb in mutate:
        extra_tags.update(_MUTATE_TO_TAGS.get(verb, ()))
    if mutate & _LIST_AFFECTING_VERBS:
        extra_tags |= set(_PAPER_LIST_TAGS)
    if extra_tags:
        cache.invalidate_tags(*extra_tags)


def invalidate_bulk_import_done() -> None:
    """Fan out invalidation to every list view after a bulk import completes.

    The fan-out covers the same tags as ``invalidate_paper_mutated`` for
    ``"inbox"`` mutations, plus the global topics + tags + scholars
    because counts and per-row facets may all have changed.
    """
    cache = get_cache()
    cache.invalidate_tags("papers_list", "topics", "tags", "scholars_list")


def invalidate_citations_refreshed(paper_id: str) -> None:
    """Drop the per-paper cache entries affected by a citations refresh.

    The ``@cached`` decorator auto-tags every per-paper entry with
    ``f"paper_id:{paper_id}"``, so a single ``invalidate_tags`` call drops
    the references cache, the paper detail, and any other per-paper
    sub-resource in one go.  The previous implementation tried to be
    precise with two exact-drops (``paper:{id}:citations`` /
    ``paper:{id}:references``), but those keys were never produced by the
    JSON-encoded ``@cached`` key format and the bug shipped silently.
    """
    cache = get_cache()
    cache.invalidate_tags(f"paper_id:{paper_id}")


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
