"""Effective-prompt resolver with a per-process TTL cache.

Every LLM-emitting call site should read its system prompt and user
template through :func:`get_system` / :func:`get_user_template` instead of
reading a module constant directly. The resolver checks the
``prompt_overrides`` table for a per-feature override; if absent, the
caller-supplied default is used.

Caching
-------
A ``(feature, kind) -> value`` map is kept in process memory with a 60s
TTL. The primary invalidation mechanism is the editor's PUT/DELETE
handler calling :func:`invalidate` synchronously after the DB commit —
within a single UI session the editor's "save → next LLM call" cycle
always sees the new value. The 60s TTL is a safety net for cross-process
scenarios (e.g. a background scheduler running summarize while the user
edits the prompt in the UI): the worst-case staleness window is 60s.

Why a process-global cache and not per-request
----------------------------------------------
Every LLM call would otherwise pay an extra DB round-trip on a tiny
PK lookup. For pipelines that issue many calls (paper_extract over a
batch, wiki compile across dozens of concepts), that's noticeable. The
cache is single-process — fine for a single-user local app.

Session argument
----------------
``session`` is optional. Streaming call sites (paper_chat, wiki_chat) pass
their request-scoped session so the override read participates in the
caller's transaction. Background job sites (summarize, topics,
wiki_enrich) pass ``None`` and the resolver opens a short-lived session
from the app engine.

Lockstep with the catalog
-------------------------
The set of valid ``feature`` strings is owned by
:mod:`carrel.prompts`. A typo on either side silently falls through to
the default (resolver lookup misses → returns ``default``). The
``test_runtime_every_call_site_feature_is_catalogued`` test in
``tests/test_prompts_runtime.py`` guards against that.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from sqlmodel import Session

from carrel.db import get_app_engine
from carrel.models import PromptOverride

_TTL_SECONDS = 60.0
_SYSTEM = "system"
_USER_TEMPLATE = "user_template"


@dataclass
class _CacheEntry:
    value: str | None  # None == no override row for this (feature, kind)
    expires_at: float


_CACHE: dict[tuple[str, str], _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()


def get_system(
    feature: str,
    default: str,
    *,
    session: Session | None = None,
) -> str:
    """Return the effective system prompt for ``feature``.

    Reads ``prompt_overrides.system`` (or falls back to ``default``).
    See module docstring for caching and session semantics.
    """
    return _resolve(_SYSTEM, feature, default, session)


def get_user_template(
    feature: str,
    default: str,
    *,
    session: Session | None = None,
) -> str:
    """Return the effective user template for ``feature``.

    Reads ``prompt_overrides.user_template`` (or falls back to ``default``).
    Returned value is the raw template (may contain ``{placeholder}``
    syntax); the call site is responsible for ``.format(**kwargs)`` as
    before.
    """
    return _resolve(_USER_TEMPLATE, feature, default, session)


def invalidate(feature: str) -> None:
    """Drop cached entries for ``feature``.

    Called by the PUT / DELETE handlers after a successful commit so the
    next LLM call sees the new value without waiting for the 60s TTL.
    No-op if there is nothing cached for ``feature``.
    """
    with _CACHE_LOCK:
        for key in list(_CACHE.keys()):
            if key[0] == feature:
                del _CACHE[key]


def _resolve(
    kind: str,
    feature: str,
    default: str,
    session: Session | None,
) -> str:
    key = (feature, kind)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit.expires_at > now:
            return default if hit.value is None else hit.value

    # Cache miss / expired — read from DB.
    override = _read_override(feature, kind, session)
    with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(value=override, expires_at=now + _TTL_SECONDS)
    return default if override is None else override


def _read_override(
    feature: str,
    kind: str,
    session: Session | None,
) -> str | None:
    """Read one column from ``prompt_overrides`` for ``(feature, kind)``.

    Returns ``None`` if no row exists or the column is NULL.
    """
    if session is not None:
        row = session.get(PromptOverride, feature)
    else:
        engine = get_app_engine()
        with Session(engine) as own:
            row = own.get(PromptOverride, feature)
    if row is None:
        return None
    return getattr(row, kind)


__all__ = ["get_system", "get_user_template", "invalidate"]
