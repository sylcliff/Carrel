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
        # Reverse index: tag -> set of keys that declared this tag.
        self._tag_index: dict[str, set[str]] = {}
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

    def set(self, key: str, value: Any, *, tags: Iterable[str] = ()) -> None:
        with self._lock:
            existing = key in self._data
            old_tags: set[str] = set()
            if existing:
                # Drop the old tag-index entries first so the new set
                # doesn't carry forward tags the caller removed.
                old_tags = self._tag_index.pop(f"__tags__:{key}", set())
                self._data.move_to_end(key)
                self._data[key] = value
            else:
                self._data[key] = value
            for t in old_tags:
                bucket = self._tag_index.get(t)
                if bucket is not None:
                    bucket.discard(key)
                    if not bucket:
                        self._tag_index.pop(t, None)
            for t in tags:
                self._tag_index.setdefault(t, set()).add(key)
            # Stash the tag list on a parallel index entry so overwrite can
            # clean up later. We use the ``__tags__:`` prefix to keep these
            # out of the tag-fan-out path.
            self._tag_index[f"__tags__:{key}"] = set(tags)
            self._evict_if_needed()

    def invalidate_exact(self, key: str) -> bool:
        """Drop a single entry by full key. Returns True if anything was removed."""
        with self._lock:
            removed = self._data.pop(key, None) is not None
            tags = self._tag_index.pop(f"__tags__:{key}", set())
            for t in tags:
                bucket = self._tag_index.get(t)
                if bucket is not None:
                    bucket.discard(key)
                    if not bucket:
                        self._tag_index.pop(t, None)
            if removed:
                self.invalidations += 1
            return removed

    def invalidate_prefix(self, prefix: str) -> int:
        """Drop every entry whose key starts with ``prefix``."""
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                self.invalidate_exact(k)
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
                self.invalidate_exact(k)
            return len(targets)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._tag_index.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "size": len(self._data),
                "maxsize": self.maxsize,
                "tags": len([t for t in self._tag_index if not t.startswith("__")]),
                "hits": self.hits,
                "misses": self.misses,
                "invalidations": self.invalidations,
                "last_status": self.last_status,
            }

    # -- internals -------------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Pop the LRU entry if we're over capacity."""
        while len(self._data) > self.maxsize:
            old_key, _ = self._data.popitem(last=False)
            tags = self._tag_index.pop(f"__tags__:{old_key}", set())
            for t in tags:
                bucket = self._tag_index.get(t)
                if bucket is not None:
                    bucket.discard(old_key)
                    if not bucket:
                        self._tag_index.pop(t, None)


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
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            cache = get_cache()
            # Map positional + keyword args into a flat dict.
            import inspect
            sig = inspect.signature(func)
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            params = {name: bound.arguments.get(name) for name in key_params}
            key = _stable_key(route, params, offset_invariant=offset_invariant)
            cached_value = cache.get(key)
            if cached_value is not None:
                # Operator-facing hit signal. Read by the /health?debug=1
                # smoke check and by ad-hoc curl probes during incident
                # debugging. Cheap to set on every request.
                cache.last_status = "HIT"
                return cached_value
            value = func(*args, **kwargs)
            cache.last_status = "MISS"
            cache.set(key, value, tags=tags)
            return value
        # Preserve a reference to the original for tests that want to call
        # through directly (bypassing the cache).
        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        wrapper.__cache_route__ = route  # type: ignore[attr-defined]
        return wrapper
    return decorator
