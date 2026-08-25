"""Long-running sync engine for a scholar's OpenAlex works list.

Mirrors :mod:`carrel.pipeline.authors` (which resolves author A-IDs for
in-library papers) and is called by two paths:

  * **lazy** — :func:`carrel.api.scholars.list_scholar_works` spawns a
    background thread on first visit when the cache table is empty.
  * **manual refresh** — :mod:`carrel.api.scholar_works_sync` creates
    a Job and runs this in a ``BackgroundTasks`` worker.

Both paths share the same engine so a refresh in progress blocks a
concurrent lazy-load from re-fetching the same author's works.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from carrel.cache import openalex_works as cache
from carrel.models import AuthorWorksSync
from carrel.sources import openalex_client as oa

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]

# Page size for the OpenAlex cursor walk. OpenAlex allows up to 200 per
# page, but a 50-row page keeps memory low and the request duration well
# under the politeness-pool budget.
_PAGE_SIZE = 50
# Polite pacing between pages. Authors with 1000+ works would otherwise
# hammer the cursor endpoint; 0.4s matches the
# :mod:`carrel.pipeline.authors` per-paper pacing.
_REQUEST_SLEEP = 0.4


def _now() -> datetime:
    return datetime.now(UTC)


def _select_works(
    session: Session, author_id: str, *, force: bool = False
) -> dict[str, int]:
    """Cursor-walk :func:`oa.fetch_author_works` and write each page to cache.

    Returns counts. The caller (``sync_scholar_works``) owns the
    AuthorWorksSync status transitions around this — keeping the
    pre-flight idempotency check + final state in one place.
    """
    counts = {"pages": 0, "works": 0, "missing": 0}
    cursor: str | None = None
    total: int | None = None
    while True:
        items, next_cursor, page_total = oa.fetch_author_works(
            author_id, cursor=cursor, limit=_PAGE_SIZE
        )
        if total is None and page_total is not None:
            total = page_total
        if not items:
            counts["pages"] += 1
            break
        # ``force`` is a placeholder for the future incremental refresh:
        # currently we re-write every page, which keeps the upsert
        # idempotent and refreshes ``fetched_at`` so the next page read
        # sees a fresh row.
        _ = force
        written = cache.upsert_works(session, author_id, items)
        counts["pages"] += 1
        counts["works"] += written
        if next_cursor is None or not str(next_cursor).strip():
            break
        cursor = str(next_cursor)
        time.sleep(_REQUEST_SLEEP)
    counts["total"] = int(total or 0)
    return counts


def _already_loading(session: Session, author_id: str) -> bool:
    """True if another worker already marked this author as ``loading``.

    Without this check, a slow first-visit fetch racing a manual refresh
    would double the OA request count for the same author.
    """
    row = session.exec(
        select(AuthorWorksSync).where(AuthorWorksSync.author_id == author_id)
    ).first()
    return bool(row and row.status == "loading")


def sync_scholar_works(
    session: Session,
    scholar_aid: str,
    *,
    on_progress: ProgressCallback | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Fetch and cache the full works list for one OpenAlex author.

    Idempotent: a concurrent call for the same author sees
    ``status='loading'`` and returns without re-fetching (unless
    ``force=True`` — used by the manual refresh path which the user
    explicitly wants re-run).

    Returns a small summary dict (``status``, ``pages``, ``works``,
    ``total``) for the caller to surface in the Job / response.
    """
    scholar_aid = (scholar_aid or "").strip()
    if not scholar_aid:
        raise ValueError("scholar_aid is required")

    if not force and _already_loading(session, scholar_aid):
        logger.info(
            "scholar_works_sync: %s already loading; skipping duplicate", scholar_aid
        )
        return {"status": "skipped", "reason": "already_loading"}

    cache.mark_sync_status(
        session, scholar_aid, "loading", total_count=None, error=None
    )
    session.commit()

    if on_progress:
        on_progress(
            {
                "scholar_aid": scholar_aid,
                "stage": "fetching",
                "detail": f"Fetching works for {scholar_aid}…",
                "pages": 0,
                "works": 0,
            }
        )

    try:
        counts = _select_works(session, scholar_aid, force=force)
    except Exception as e:  # noqa: BLE001
        logger.exception("scholar_works_sync: %s failed", scholar_aid)
        cache.mark_sync_status(
            session, scholar_aid, "failed", error=str(e)[:500]
        )
        session.commit()
        if on_progress:
            on_progress(
                {
                    "scholar_aid": scholar_aid,
                    "stage": "failed",
                    "detail": str(e)[:200],
                    **counts,
                }
            )
        raise

    cache.mark_sync_status(
        session,
        scholar_aid,
        "ready",
        total_count=counts.get("total"),
    )
    # Stamp last_full_sync_at directly — the status update helper only
    # touches ``status``/``total_count``/``last_error``/``updated_at``;
    # we need this so the next manual refresh knows how stale the cache
    # is. ``incremental`` stays untouched (not used in this PR).
    row = session.get(AuthorWorksSync, scholar_aid)
    if row is not None:
        row.last_full_sync_at = _now()
        session.add(row)
    session.commit()

    if on_progress:
        on_progress(
            {
                "scholar_aid": scholar_aid,
                "stage": "done",
                "detail": (
                    f"Fetched {counts.get('works', 0)} works "
                    f"across {counts.get('pages', 0)} pages"
                ),
                **counts,
            }
        )
    logger.info(
        "scholar_works_sync: %s done — %d pages, %d works (total=%s)",
        scholar_aid,
        counts.get("pages", 0),
        counts.get("works", 0),
        counts.get("total"),
    )
    return {"status": "ready", **counts}
