"""HTTP cache-header helpers (Layer 1).

Three primitives:

- :func:`etag_for_updated_at` — build a stable, collision-resistant ETag from a
  row's ``updated_at`` timestamp (and, for list endpoints, the row set).
- :func:`apply_etag_headers` — set the ``ETag`` + ``Cache-Control`` headers on
  a FastAPI response.
- :func:`if_none_match_matches` — check the request's ``If-None-Match`` against
  the current ETag; the caller returns 304 if this returns ``True``.

Why not the well-known :mod:`fastapi_cache`? It pulls in a redis client and
configures a backend. We want zero new infrastructure and we want the ETag to
be derivable from data we already loaded for the response (so it is "free").
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Iterable

from fastapi import Request, Response

# A short prefix that means "weak ETag" per RFC 7232 §2.3. Carrel's responses
# are byte-for-byte reproducible for a given row, so strong would also be
# correct, but weak is conventional for ETag derived from semantic version.
_WEAK_PREFIX = 'W/"'


def etag_for_updated_at(
    updated_at: datetime | None,
    *,
    extra: Iterable[str] = (),
) -> str | None:
    """Build a weak ETag from a row's ``updated_at`` timestamp.

    Returns ``None`` when ``updated_at`` is ``None`` (caller should skip ETag
    logic in that case — the row hasn't been written yet, so caching has no
    useful semantics).

    The format is::

        W/"{id_or_digest}-{iso_timestamp}"

    where ``id_or_digest`` is a stable identifier of the row or row-set, and
    ``iso_timestamp`` is ``updated_at.isoformat()``. Including the id makes
    the ETag globally unique even if two rows share the same timestamp, and
    including the timestamp ensures the ETag changes on every update.
    """
    if updated_at is None:
        return None
    # microsecond precision: Postgres timestamptz stores microseconds, so
    # collisions within a single row are impossible at this resolution.
    iso = updated_at.isoformat()
    if extra:
        # Sort for stable hash; the row-set identity is part of the ETag so
        # a deleted-then-re-inserted row with the same id does not 304.
        digest = hashlib.sha1("|".join(sorted(extra)).encode("utf-8")).hexdigest()[:12]
        return f'{_WEAK_PREFIX}{digest}-{iso}"'
    return f'{_WEAK_PREFIX}{iso}"'


def etag_for_list(
    *,
    max_updated_at: datetime | None,
    row_ids: Iterable[str],
    count: int,
) -> str | None:
    """Build an ETag for a paginated list response.

    The fingerprint is the SHA-1 of the sorted row ids, so a list with the same
    max timestamp but a different row set gets a different ETag. The
    ``count`` is mixed in as a final safety belt.

    For the empty list (``max_updated_at is None and count == 0``) a fixed
    ETag is returned so a fresh-install /library response can still 304
    on subsequent visits. The first row added produces a different
    ``(count, ids, timestamp)`` triple and therefore a different ETag,
    so the transition is naturally visible.
    """
    if max_updated_at is None and count == 0:
        return f'{_WEAK_PREFIX}0:empty:empty"'
    ids_digest = hashlib.sha1(
        "".join(sorted(row_ids)).encode("utf-8"),
    ).hexdigest()[:12]
    iso = max_updated_at.isoformat() if max_updated_at else "empty"
    return f'{_WEAK_PREFIX}{count}:{ids_digest}:{iso}"'


def _cache_control_value(*, max_age: int, stale_while_revalidate: int) -> str:
    """Build the ``Cache-Control`` literal. Single source of truth — both
    the 200 path (:func:`apply_etag_headers`) and the 304 path
    (:func:`maybe_return_304`) route through here so a future change to
    the literal (e.g. adding ``must-revalidate``) can never drift
    between the two responses."""
    if stale_while_revalidate > 0:
        return (
            f"private, max-age={max_age}, "
            f"stale-while-revalidate={stale_while_revalidate}"
        )
    return f"private, max-age={max_age}"


def apply_etag_headers(
    response: Response,
    etag: str,
    *,
    max_age: int,
    stale_while_revalidate: int = 0,
) -> None:
    """Set the standard caching headers on a :class:`Response`.

    ``max_age=0`` + ``stale-while-revalidate=N`` is the standard "revalidate
    but allow stale" pattern. ``max_age=0`` alone is "must revalidate" which
    is what we use for paper detail (we always want to know if the row
    changed). The ``private`` directive prevents shared caches (and any
    forward proxy) from storing user-specific responses.
    """
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = _cache_control_value(
        max_age=max_age, stale_while_revalidate=stale_while_revalidate
    )


def maybe_return_304(
    request: Request,
    etag: str | None,
    *,
    max_age: int,
    stale_while_revalidate: int = 0,
) -> Response | None:
    """Return a 304 ``Response`` if the request's ``If-None-Match`` matches.

    Returns ``None`` when the caller should serve the 200 body and apply
    the matching ``ETag`` / ``Cache-Control`` via :func:`apply_etag_headers`.
    Use this in a read route to collapse the 304 / 200 branch into two
    short lines::

        if (r := maybe_return_304(request, etag, max_age=60, swr=120)):
            return r
        apply_etag_headers(response, etag, max_age=60, swr=120)
        return body

    The ``etag`` is allowed to be ``None`` (caller did not produce one);
    in that case we always fall through to the 200 path.
    """
    if etag is None or not if_none_match_matches(request, etag):
        return None
    return Response(
        status_code=304,
        headers={
            "ETag": etag,
            "Cache-Control": _cache_control_value(
                max_age=max_age, stale_while_revalidate=stale_while_revalidate
            ),
        },
    )


def if_none_match_matches(request: Request, etag: str) -> bool:
    """Return True if the request's ``If-None-Match`` matches the given ETag.

    Per RFC 7232 §3.2, ``If-None-Match: *`` matches when the resource exists
    at all, and a comma-separated list of ETags matches if any one of them
    matches. We accept both forms.
    """
    header = request.headers.get("if-none-match")
    if not header:
        return False
    if header.strip() == "*":
        return True
    # RFC 7232 §2.3.2 weak comparison: the only difference between
    # ``W/"a"`` and ``"a"`` is the weak prefix.  Use ``startswith`` on the
    # prefix and byte-equality on the opaque-tag payload — ``endswith``
    # is a byte-substring check and would let a prefix ETag match a
    # different (longer) one (e.g. ``W/"a"`` matching ``W/"abc"``).
    server_opaque = etag[len(_WEAK_PREFIX):-1] if etag.startswith(_WEAK_PREFIX) else etag[1:-1]
    candidates = [c.strip() for c in header.split(",")]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate.startswith(_WEAK_PREFIX) and candidate.endswith('"'):
            cand_opaque = candidate[len(_WEAK_PREFIX):-1]
        elif candidate.startswith('"') and candidate.endswith('"'):
            cand_opaque = candidate[1:-1]
        else:
            continue
        if cand_opaque == server_opaque:
            return True
    return False
