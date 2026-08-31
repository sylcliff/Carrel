"""In-process application cache (Layer 2).

A thread-safe LRU keyed by ``"route:key_params#tag1,tag2"`` with a parallel
tag index for fan-out invalidation. Backs the :func:`cached` decorator that
read-only routes use to memoize their responses.

Design constraints:

- No new dependency (``cachetools`` is not in the project).
- Single-process: Carrel is one worker. If we ever go multi-worker, this
  layer needs to move to Redis or similar.
- Memory bound: ``maxsize=512`` by default. Each entry is a single
  serializable object (typically a Pydantic model list or a markdown
  string), so worst-case memory is ~25 MB.
- Thread-safe: reads and writes take a single re-entrant lock. The critical
  sections are tiny (a dict update or a set update), so contention is
  negligible at single-user scale.
- Fan-out invalidation: the decorator requires every entry to declare its
  ``tags``; ``invalidate_tags("papers_list", ...)`` drops every entry that
  declared any of those tags. The fan-out is O(entries-tagged) per call,
  which is fine because tags are few and entries are few.

The key grammar is::

    {route}:{key_params}#{tags}

The ``route`` segment is a short identifier like ``paper`` or
``papers_list``. The ``key_params`` segment is a deterministic string built
from the request inputs (filter dict, paper id, etc.). The ``tags`` segment
is a comma-separated list used only for invalidation fan-out — two entries
with the same route+key_params and different tags are considered equal and
the second ``set()`` overwrites the first.
"""
from __future__ import annotations

import inspect
import json
import threading
from collections import OrderedDict
from typing import Any, Callable, Iterable

# The global cache. Imported by routes and the invalidation helpers.
# ``maxsize=512`` is the documented upper bound; see the plan's R4 risk.
_cache = None
_lock = threading.RLock()


def get_cache() -> "AppCache":
    """Return the process-wide :class:`AppCache` singleton, creating it on first use."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = AppCache(maxsize=512)
        return _cache


def reset_cache_for_tests() -> None:
    """Drop the singleton so the next :func:`get_cache` returns a fresh one.

    Tests call this between cases to avoid cross-test pollution.
    """
    global _cache
    with _lock:
        _cache = None


class AppCache:
    """Thread-safe LRU + tag-indexed fan-out invalidation."""

    def __init__(self, *, maxsize: int = 512) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        # LRU: most-recently-used at the back.
        self._data: "OrderedDict[str, Any]" = OrderedDict()
        # Forward index: tag -> set of keys that declared this tag.
        # Used for invalidate_tags fan-out.
        self._tag_index: dict[str, set[str]] = {}
        # Reverse index: key -> frozenset of tags that key declared.
        # Kept in a side dict (not in _data) so the OrderedDict doesn't
        # carry parallel "metadata" slots that get out of order with the
        # LRU move-to-end.
        self._key_tags: dict[str, frozenset[str]] = {}
        self._lock = threading.RLock()
        # Stats for /health?debug=1.
        self.hits = 0
        self.misses = 0
        self.invalidations = 0
        # Last-touch status — flipped by the @cached decorator. "COLD" if
        # no decorator call has run yet, or after an explicit clear().
        self.last_status = "COLD"

    # -- core ops --------------------------------------------------------

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key not in self._data:
                self.misses += 1
                return None
            self.hits += 1
            self._data.move_to_end(key)
            return self._data[key]

    def has(self, key: str) -> bool:
        """True if ``key`` is currently in the cache (even if its stored
        value is ``None``). Lets the :func:`cached` decorator distinguish a
        miss from a cached ``None`` — a legit return for routes that report
        "not extracted yet" or "not in library"."""
        with self._lock:
            return key in self._data

    def set(self, key: str, value: Any, *, tags: Iterable[str] = ()) -> None:
        with self._lock:
            old_tags = self._key_tags.pop(key, frozenset())
            self._data[key] = value
            self._data.move_to_end(key)
            self._unindex_tags(key, old_tags)
            new_tags = frozenset(tags)
            if new_tags:
                self._key_tags[key] = new_tags
                for t in new_tags:
                    self._tag_index.setdefault(t, set()).add(key)
            self._evict_if_needed()

    def invalidate_exact(self, key: str) -> bool:
        """Drop a single entry by full key. Returns True if anything was removed."""
        with self._lock:
            return self._evict_under_lock(key)

    def invalidate_prefix(self, prefix: str) -> int:
        """Drop every entry whose key starts with ``prefix``."""
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                self._evict_under_lock(k)
            return len(keys)

    def invalidate_tags(self, *tags: str) -> int:
        """Drop every entry that declared any of the given tags.

        Returns the total number of entries removed. An entry can declare
        multiple tags, so it is removed at most once even if it matches
        several.

        Note: passing a tag is global. The caller must have used that tag
        on every entry that should be evicted. For per-id invalidation
        prefer :meth:`invalidate_exact`.
        """
        with self._lock:
            # Collect the unique set of keys across all tags.
            targets: set[str] = set()
            for t in tags:
                bucket = self._tag_index.get(t)
                if bucket:
                    targets.update(bucket)
            for k in targets:
                self._evict_under_lock(k)
            return len(targets)

    def invalidate_many(self, keys: Iterable[str]) -> int:
        """Drop a batch of exact keys in a single lock acquisition.

        Useful when a single write path has to invalidate N per-id
        entries (e.g. dropping a tag attached to N papers). Each key
        is processed via :meth:`_evict_under_lock`, but the lock is
        taken once, not N times.
        """
        with self._lock:
            removed = 0
            for k in keys:
                if self._evict_under_lock(k):
                    removed += 1
            return removed

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._tag_index.clear()
            self._key_tags.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "size": len(self._data),
                "maxsize": self.maxsize,
                "tags": len(self._tag_index),
                "hits": self.hits,
                "misses": self.misses,
                "invalidations": self.invalidations,
                "last_status": self.last_status,
            }

    # -- internals -------------------------------------------------------

    def _unindex_tags(self, key: str, tags: Iterable[str]) -> None:
        """Remove ``key`` from every tag bucket listed in ``tags``."""
        for t in tags:
            bucket = self._tag_index.get(t)
            if bucket is None:
                continue
            bucket.discard(key)
            if not bucket:
                self._tag_index.pop(t, None)

    def _evict_under_lock(self, key: str) -> bool:
        """Pop ``key`` and its tag-index entries; caller holds the lock."""
        if self._data.pop(key, None) is None:
            return False
        self._unindex_tags(key, self._key_tags.pop(key, ()))
        self.invalidations += 1
        return True

    def _evict_if_needed(self) -> None:
        """Pop LRU entries if we're over capacity."""
        while len(self._data) > self.maxsize:
            old_key, _ = self._data.popitem(last=False)
            self._unindex_tags(old_key, self._key_tags.pop(old_key, ()))


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def _stable_key(
    route: str,
    params: dict[str, Any],
    *,
    offset_invariant: bool,
) -> str:
    """Build a stable cache key from the route and request params.

    Filter values are JSON-serialized with sorted keys so e.g.
    ``{"tag": ["a"], "q": "x"}`` and ``{"q": "x", "tag": ["a"]}`` produce
    the same key.

    If ``offset_invariant`` is True (the default), the ``offset`` key is
    included in the cache key. Pass ``offset_invariant=False`` to *exclude*
    the offset from the key — useful when the value at offset > 0 is so
    rarely revisited that caching it wastes memory. The recommended
    setting: ``offset_invariant=True`` for endpoints that only ever serve
    the first page; ``False`` for cursor-paginated ones.
    """
    if offset_invariant and "offset" in params:
        # Include the offset so different pages cache separately.
        payload = dict(sorted(params.items()))
    else:
        # Drop the offset from the key — caller asked for it.
        payload = {k: v for k, v in params.items() if k != "offset"}
        payload = dict(sorted(payload.items()))
    return f"{route}:{json.dumps(payload, separators=(',', ':'), default=str)}"


# Key-param names that uniquely identify a row. When a ``@cached`` route
# declares one of these in ``key_params``, the decorator auto-attaches a
# per-id tag of the form ``"{name}:{value}"`` so per-id invalidation can
# be a single ``invalidate_tags`` call — no exact-key knowledge required,
# no risk of drift between the invalidation helper and the cache key
# format, and any future per-row sub-resource (paper_card, paper_chat, …)
# participates automatically as long as it puts the id in key_params.
_ID_LIKE_PARAMS = frozenset(
    {"paper_id", "page_id", "tag_id", "scholar_aid", "aid", "topic_id"}
)


def cached(
    route: str,
    *,
    key_params: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    offset_invariant: bool = True,
) -> Callable:
    """Memoize a read-only function in :func:`get_cache`.

    ``key_params`` is the tuple of argument names whose values feed the cache
    key. ``tags`` declares which invalidation buckets the entry belongs to.

    Usage::

        @cached("paper", key_params=("paper_id",), tags=("paper",))
        def get_paper(paper_id: str) -> PaperDetail:
            ...

    The wrapped function must be a regular sync function. Async functions
    would require awaiting the cache inside the function — the decorator
    can't await on their behalf.
    """
    def decorator(func: Callable) -> Callable:
        # Bind the signature once at decoration time so the hot path
        # only does a cheap bind_partial per request, not a full
        # inspect.signature scan.
        sig = inspect.signature(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            cache = get_cache()
            bound = sig.bind_partial(*args, **kwargs)
            params = {name: bound.arguments.get(name) for name in key_params}
            key = _stable_key(route, params, offset_invariant=offset_invariant)
            # ``has`` (not ``get() is not None``) so a legit ``None`` return
            # is memoized: ``_get_paper_body`` for an id with no row returns
            # ``None`` and should be cached until the row is inserted.
            if cache.has(key):
                cached_value = cache.get(key)
                # Operator-facing hit signal. Read by the /health?debug=1
                # smoke check and by ad-hoc curl probes during incident
                # debugging. Cheap to set on every request.
                cache.last_status = "HIT"
                return cached_value
            value = func(*args, **kwargs)
            cache.last_status = "MISS"
            # Per-id fan-out tags: one ``f"{name}:{value}"`` per id-like
            # key_param. Lets the invalidation helper drop a single
            # row's caches across all sub-resources with one
            # ``invalidate_tags`` call. Only string-typed values are
            # tagged (lists / dicts / None would explode the tag set).
            per_id_tags = tuple(
                f"{name}:{val}"
                for name, val in params.items()
                if name in _ID_LIKE_PARAMS and isinstance(val, str)
            )
            cache.set(key, value, tags=tuple(tags) + per_id_tags)
            return value
        # Preserve a reference to the original for tests that want to call
        # through directly (bypassing the cache).
        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        wrapper.__cache_route__ = route  # type: ignore[attr-defined]
        return wrapper
    return decorator
