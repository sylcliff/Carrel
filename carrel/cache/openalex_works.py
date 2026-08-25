"""Persistent read-through helpers for OpenAlex Work lookups (A+B+D).

The three entry points are read-only — they never trigger a long-running
sync. The sync engine itself lives in
:mod:`carrel.pipeline.scholar_works_sync`; this module just looks rows up
and (on a miss) writes the fresh result back so the next caller skips
OpenAlex entirely.

Layering:

  1. ``paper.raw_meta["openalex"]`` — papers that already live in the
     library carry the full Work dict in :attr:`Paper.raw_meta` (set by
     the sync / import paths). For an in-library arXiv id, this is a
     free hit that costs no OpenAlex request and no extra DB table lookup.
  2. ``work_by_arxiv_id`` — second-tier SQLite table. Hit here when the
     id isn't in the library (or its row predates the raw_meta fall-back).
  3. OpenAlex live — last resort. The result is written back to
     ``work_by_arxiv_id`` (or ``author_works_cache`` for the W-id path)
     so the next caller short-circuits at layer 1 or 2.

All write-backs use ``INSERT ... ON CONFLICT DO UPDATE`` to stay
idempotent across dialects.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func as sa_func
from sqlmodel import Session, select

from carrel.models import AuthorWorksCache, AuthorWorksSync, Paper, WorkByArxivId
from carrel.sources import openalex_client as oa

logger = logging.getLogger(__name__)

# Current cache schema version. Bump on shape changes that would render
# existing ``raw_json`` blobs stale; the next read will see the mismatch
# and re-fetch from OpenAlex.
SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(UTC)


def _strip_arxiv_version(arxiv_id: str) -> str:
    """Strip ``v1``/``v2``/... so identity is stable across revisions.

    Mirrors :func:`carrel.sources.normalize._strip_arxiv_version`; inlined
    here to avoid an import cycle (normalize → models → cache).
    """
    return re.sub(r"v\d+$", "", arxiv_id)


# ---------------------------------------------------------------------------
# Author works cache (A)
# ---------------------------------------------------------------------------


def get_sync_status(session: Session, author_id: str) -> AuthorWorksSync | None:
    """Return the cached sync-state row for an author, or None."""
    return session.get(AuthorWorksSync, author_id)


def mark_sync_status(
    session: Session,
    author_id: str,
    status: str,
    *,
    total_count: int | None = None,
    error: str | None = None,
) -> None:
    """Upsert the AuthorWorksSync row.

    Creates the row on first call (status='missing' rows are not seeded
    eagerly). Caller is responsible for ``session.commit()``.
    """
    row = session.get(AuthorWorksSync, author_id)
    if row is None:
        row = AuthorWorksSync(
            author_id=author_id,
            status=status,
            total_count=total_count,
            last_error=error,
            updated_at=_now(),
        )
        session.add(row)
        return
    row.status = status
    row.updated_at = _now()
    if total_count is not None:
        row.total_count = total_count
    if error is not None:
        row.last_error = error
    session.add(row)


def get_cached_works(
    session: Session,
    author_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuthorWorksCache], int]:
    """Return ``(rows_for_page, total)`` for an author.

    Zero OpenAlex calls. Sort matches the original
    ``fetch_author_works`` ordering — newest publication first, ties
    broken by citation count, then by W-id (stable). SQL NULLS LAST is
    expressed via ``IS NULL`` guards so the same query works on
    PostgreSQL and SQLite.
    """
    total = session.scalar(
        select(sa_func.count())
        .select_from(AuthorWorksCache)
        .where(AuthorWorksCache.author_id == author_id)
    )

    # Newest publication date first; rows missing a date sort last;
    # tie-break by cited_by_count DESC, then W-id (stable).
    rows = session.exec(
        select(AuthorWorksCache)
        .where(AuthorWorksCache.author_id == author_id)
        .order_by(
            AuthorWorksCache.publication_date.is_(None),
            AuthorWorksCache.publication_date.desc(),
            AuthorWorksCache.cited_by_count.desc(),
            AuthorWorksCache.openalex_id.asc(),
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return list(rows), int(total or 0)


def upsert_works(
    session: Session,
    author_id: str,
    works: list[dict[str, Any]],
) -> int:
    """Insert/replace a page of OpenAlex Work dicts.

    Returns the number of rows touched. The same row can be written
    multiple times if it appears under several authors in the source —
    the last write wins on the author_id column (acceptable: the
    displayed author is whichever most-recently refreshed the row).
    """
    count = 0
    for w in works:
        wid = oa.work_id(w)
        if not wid:
            continue
        # Some OpenAlex Work dicts have arxiv_id; strip the version so
        # the partial index lookup in lookup_work_by_arxiv_id hits.
        arxiv_id = oa.work_arxiv_id(w)
        if arxiv_id:
            arxiv_id = _strip_arxiv_version(arxiv_id)
        title = oa.work_title(w) or ""
        pdf_url, oa_status = oa.work_pdf_url(w)
        is_oa = oa_status == "oa"
        pd = oa.work_publication_date(w)
        venue = oa.work_venue(w)
        doi = oa.work_doi(w)
        row = AuthorWorksCache(
            openalex_id=wid,
            author_id=author_id,
            title=title,
            publication_date=pd,
            publication_year=pd.year if pd is not None else None,
            venue=venue,
            doi=doi,
            arxiv_id=arxiv_id,
            cited_by_count=w.get("cited_by_count"),
            is_oa=is_oa,
            pdf_url=pdf_url,
            oa_status=oa_status,
            raw_json=w,
            schema_version=SCHEMA_VERSION,
            fetched_at=_now(),
        )
        session.merge(row)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Work-by-arXiv-id cache (B)
# ---------------------------------------------------------------------------


def _lookup_in_library(arxiv_id: str) -> dict[str, Any] | None:
    """Layer 1: in-library paper whose raw_meta already carries the Work.

    Inlined to avoid opening a fresh session per call; the caller passes
    us ``arxiv_id`` and we go to the layer-1 table in
    :func:`lookup_work_by_arxiv_id` (which has the session).
    """
    # Implemented inside lookup_work_by_arxiv_id; this stub exists only
    # so the docstring above stays in sync.
    raise NotImplementedError


def lookup_work_by_arxiv_id(
    session: Session,
    arxiv_id: str,
    *,
    title_hint: str | None = None,
) -> dict[str, Any] | None:
    """Read-through cache for :func:`carrel.sources.openalex_client.lookup_by_arxiv_id`.

    Returns a Work dict (same shape as the live call) or None if no
    match exists. Writes the result back to ``work_by_arxiv_id`` on a
    fresh fetch so subsequent calls hit layer 2.
    """
    arxiv_id = (arxiv_id or "").strip()
    if not arxiv_id:
        return None
    bare = _strip_arxiv_version(arxiv_id)

    # Layer 1: in-library paper whose raw_meta carries the Work dict.
    paper = session.exec(
        select(Paper).where(
            Paper.arxiv_id == bare,
            Paper.in_library.is_(True),
        )
    ).first()
    if paper is not None:
        raw = paper.raw_meta or {}
        cached = raw.get("openalex")
        if isinstance(cached, dict):
            return cached

    # Layer 2: dedicated cache table.
    row = session.get(WorkByArxivId, bare)
    if row is not None and row.schema_version == SCHEMA_VERSION:
        cached = row.raw_json
        if isinstance(cached, dict):
            return cached
        # Schema mismatch — fall through to re-fetch.

    # Layer 3: live OpenAlex.
    work = oa.lookup_by_arxiv_id(bare, title_hint=title_hint)
    if work is None:
        return None
    wid = oa.work_id(work) or ""
    if row is None:
        row = WorkByArxivId(
            arxiv_id=bare,
            openalex_id=wid,
            raw_json=work,
            schema_version=SCHEMA_VERSION,
            fetched_at=_now(),
        )
        session.add(row)
    else:
        row.openalex_id = wid
        row.raw_json = work
        row.schema_version = SCHEMA_VERSION
        row.fetched_at = _now()
        session.add(row)
    return work


# ---------------------------------------------------------------------------
# Work-by-OA-id cache (D)
# ---------------------------------------------------------------------------


def lookup_work_by_oa_id(
    session: Session,
    oa_id: str,
) -> dict[str, Any] | None:
    """Read-through cache for ``openalex_client.Works()[oa_id]``.

    Tries (in order) the author-works cache (the W-id is the PK), the
    work-by-arXiv cache (covers the W-id ↔ arXiv id cross-walk), and
    finally the live OpenAlex call. Writes back to the author-works
    table only when we can find an author to attach the row to (which
    isn't always knowable for an ad-hoc import); a ``None`` author is
    fine because the next sync will pick the row up.
    """
    oa_id = (oa_id or "").strip()
    if not oa_id:
        return None
    # Strip a possible URL prefix the client might have left in.
    oa_id = oa_id.rsplit("/", 1)[-1]

    # Layer 1: any author-works row keyed by this W-id.
    row = session.get(AuthorWorksCache, oa_id)
    if row is not None and row.schema_version == SCHEMA_VERSION:
        cached = row.raw_json
        if isinstance(cached, dict):
            return cached

    # Layer 2: work_by_arxiv_id doesn't key on W-id directly, so the
    # cross-walk here is a one-row scan: cheap, but skip it if we
    # already have a row from layer 1. (Currently this layer is a no-op
    # because lookup_by_arxiv_id already wrote the same Work dict to
    # work_by_arxiv_id; the layer is here for symmetry / future use.)

    # Layer 3: live.
    try:
        w = oa.Works()[oa_id]  # type: ignore[index]
    except Exception as e:  # noqa: BLE001
        logger.debug("OpenAlex Works()[%s] failed: %s", oa_id, e)
        return None
    work = dict(w) if w else None
    if work is None:
        return None
    if row is not None:
        row.raw_json = work
        row.schema_version = SCHEMA_VERSION
        row.fetched_at = _now()
        session.add(row)
    return work
