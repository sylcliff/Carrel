"""Scholar browse endpoints — aggregate authors across in-library papers.

Authors are stored only as a JSON list on each Paper (``[{name,
openalex_author_id, affiliation}]``); there is no Author table. These routes
aggregate that column at request time (the library is personal-scale). Authors
with an OpenAlex Author ID are grouped by that ID; arXiv/S2 records without
one fall back to exact-name matching (``key = "name:<name>"``).

The pure aggregation lives in
:mod:`carrel.pipeline.wiki._scholars_agg` (shared with the wiki compiler); this
module adds a short-TTL response cache for the list view.

``GET /scholars`` lists authors ranked by local paper count (the "important"
signal). ``GET /scholars/{key}`` returns the author's in-library papers plus,
when an OpenAlex ID is available, a live OpenAlex profile (works_count,
h_index, ...) fetched on demand and cached in-process, and any compiled wiki
page (M8).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from carrel.api.papers import _to_summary
from carrel.db import get_session_dep
from carrel.models import Paper, WikiKind, WikiPage
from carrel.pipeline.wiki import _slug
from carrel.pipeline.wiki._scholars_agg import (
    NAME_KEY_PREFIX,
    aggregate,
    get_profile,
    papers_for_key,
)
from carrel.schemas import ScholarDetail, ScholarSummary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scholars", tags=["scholars"])

# ---- aggregation cache (list view; invalidated when papers change) --------

_LIST_TTL = 60.0
_list_cache: dict[str, Any] = {"ts": 0.0, "sig": None, "items": []}
_list_lock = threading.Lock()


def _library_signature(session: Session) -> Any:
    """Cheap change-detection token: max updated_at + count of library papers."""
    row = session.exec(
        select(Paper.updated_at, Paper.id)
        .where(Paper.in_library.is_(True), Paper.discarded.is_(False))
        .order_by(Paper.updated_at.desc())
        .limit(1)
    ).first()
    count = session.exec(
        select(Paper.id).where(
            Paper.in_library.is_(True), Paper.discarded.is_(False)
        )
    ).all()
    return (row[0].isoformat() if row and row[0] else None, len(count))


def _get_scholars(session: Session) -> list[ScholarSummary]:
    """Return cached aggregation, rebuilding when stale or changed."""
    now = time.monotonic()
    with _list_lock:
        if (
            now - _list_cache["ts"] < _LIST_TTL
            and _list_cache["sig"] is not None
        ):
            return _list_cache["items"]
    sig = _library_signature(session)
    with _list_lock:
        if (
            now - _list_cache["ts"] < _LIST_TTL
            and _list_cache["sig"] == sig
        ):
            return _list_cache["items"]
        items = aggregate(session)
        _list_cache.update(ts=now, sig=sig, items=items)
        return items


def _scholar_wiki_page(session: Session, key: str, name: str) -> WikiPage | None:
    """The compiled scholar WikiPage for an aggregation key, if any."""
    if not key.startswith(NAME_KEY_PREFIX):
        page = session.exec(
            select(WikiPage).where(
                WikiPage.kind == WikiKind.scholar.value,
                WikiPage.scholar_aid == key,
            )
        ).first()
        if page:
            return page
    slug = _slug.scholar_slug(
        None if key.startswith(NAME_KEY_PREFIX) else key, name
    )
    return session.exec(
        select(WikiPage).where(
            WikiPage.kind == WikiKind.scholar.value, WikiPage.slug == slug
        )
    ).first()


@router.get("", response_model=list[ScholarSummary])
def list_scholars(
    q: str | None = Query(None, description="Case-insensitive substring match on name"),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session_dep),
) -> list[ScholarSummary]:
    items = _get_scholars(session)
    if q:
        needle = q.strip().lower()
        if needle:
            items = [s for s in items if needle in s.name.lower()]
    return items[:limit]


@router.get("/{key}", response_model=ScholarDetail)
def get_scholar(
    key: str,
    session: Session = Depends(get_session_dep),
) -> ScholarDetail:
    # Locate the matching summary from the aggregated list (handles display
    # name, affiliation, counts consistently with the browse page).
    summary = next((s for s in _get_scholars(session) if s.key == key), None)
    if summary is None:
        # Aggregation only covers in-library papers; a stale cache or an unknown
        # key means there is nothing to show.
        raise HTTPException(status_code=404, detail="scholar not found in library")

    papers = papers_for_key(session, key)

    from carrel.api.wiki import _page_detail  # local: avoid circular import

    page = _scholar_wiki_page(session, key, summary.name)
    wiki_detail = _page_detail(session, page) if page is not None else None

    return ScholarDetail(
        scholar=summary,
        papers=[_to_summary(p) for p in papers],
        profile=get_profile(key),
        wiki_page=wiki_detail,
    )
