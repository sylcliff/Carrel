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

``GET /scholars/{key}/works`` pages the OpenAlex works authored by this
scholar (newest first), joined with the local library so the UI can show an
"In library" badge or an "Import" button next to each work.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlmodel import Session, col, select

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
from carrel.schemas import (
    ScholarDetail,
    ScholarSummary,
    ScholarWorkOut,
    ScholarWorksResponse,
)
from carrel.sources import openalex_client as oa

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
    """The compiled scholar WikiPage for an aggregation key, if any.

    Looked up by ``entity_key`` (unique per scholar) so a name-only author
    who later acquired an A-ID lands on the canonical page.  Redirect
    shells are excluded — we want the live page or nothing.

    A slug fallback exists for legacy rows whose ``entity_key`` was never
    populated; those get cleaned up by the next reconcile pass.
    """
    if key.startswith(NAME_KEY_PREFIX):
        entity_key = f"scholar:name:{key[len(NAME_KEY_PREFIX):]}"
    else:
        entity_key = f"scholar:{key}"
    page = session.exec(
        select(WikiPage).where(
            WikiPage.entity_key == entity_key,
            WikiPage.redirects_to.is_(None),
        )
    ).first()
    if page is not None:
        return page
    slug = _slug.scholar_slug(
        None if key.startswith(NAME_KEY_PREFIX) else key, name
    )
    return session.exec(
        select(WikiPage).where(
            WikiPage.kind == WikiKind.scholar.value,
            WikiPage.slug == slug,
            WikiPage.redirects_to.is_(None),
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


# ---------------------------------------------------------------------------
# Published works (OpenAlex) — paginated, joined with the local library.
# ---------------------------------------------------------------------------


def _work_year(work: dict[str, Any]) -> int | None:
    """Extract a year int from a pyalex work dict (publication_date or fallback)."""
    pd = oa.work_publication_date(work)
    if pd is not None:
        return pd.year
    # OpenAlex sometimes exposes publication_year as a top-level int.
    raw = work.get("publication_year")
    if isinstance(raw, int):
        return raw
    return None


def _batch_library_match(
    session: Session, works: list[dict[str, Any]]
) -> dict[str, Paper]:
    """For each OpenAlex work in ``works``, return a Paper row if it is
    already known locally (library or inbox). Matched by any of openalex id,
    DOI (case-insensitive), or arXiv id.

    Returns ``{openalex_work_id: Paper}`` — keyed by the *OpenAlex* W-id (the
    same string the caller already has) so the result is a simple lookup.
    """
    oa_ids = {w_id for w_id in (oa.work_id(w) for w in works) if w_id}
    dois = {d.lower() for d in (oa.work_doi(w) for w in works) if d}
    arxiv_ids = {a for a in (oa.work_arxiv_id(w) for w in works) if a}
    if not oa_ids and not dois and not arxiv_ids:
        return {}

    conditions = []
    if oa_ids:
        conditions.append(col(Paper.id).in_(oa_ids))
    if dois:
        conditions.append(func.lower(col(Paper.doi)).in_(dois))
    if arxiv_ids:
        conditions.append(col(Paper.arxiv_id).in_(arxiv_ids))
    rows = session.exec(select(Paper).where(or_(*conditions))).all()

    # Build a small index of the identifiers we collected so a row can be
    # matched to its OpenAlex work from any of the three possible keys.
    by_oa: dict[str, Paper] = {r.id: r for r in rows if r.id_kind == "openalex"}
    by_doi: dict[str, Paper] = {(r.doi or "").lower(): r for r in rows if r.doi}
    by_arxiv: dict[str, Paper] = {r.arxiv_id: r for r in rows if r.arxiv_id}

    out: dict[str, Paper] = {}
    for w in works:
        wid = oa.work_id(w)
        if not wid:
            continue
        match = (
            by_oa.get(wid)
            or by_doi.get((oa.work_doi(w) or "").lower())
            or by_arxiv.get(oa.work_arxiv_id(w) or "")
        )
        if match is not None:
            out[wid] = match
    return out


@router.get("/{key}/works", response_model=ScholarWorksResponse)
def list_scholar_works(
    key: str,
    cursor: str | None = Query(
        None,
        description="Opaque OpenAlex next-cursor from the previous page",
    ),
    limit: int = Query(25, ge=1, le=50),
    session: Session = Depends(get_session_dep),
) -> ScholarWorksResponse:
    """OpenAlex works authored by this scholar (newest first), paginated.

    Each item is annotated with ``in_library`` (and ``library_id``) by
    matching against the local Paper table. Only A-ID scholars can be
    resolved through OpenAlex — name-only authors (no A-ID) return 422.
    """
    # Mirror /scholars/{key}: only authors that exist in the local aggregation
    # are addressable here. This blocks casual enumeration of OpenAlex from
    # any A-ID someone types in the URL bar.
    summary = next((s for s in _get_scholars(session) if s.key == key), None)
    if summary is None:
        raise HTTPException(status_code=404, detail="scholar not found in library")

    if key.startswith(NAME_KEY_PREFIX):
        raise HTTPException(
            status_code=422,
            detail=(
                "This author is matched by name only and has no OpenAlex Author "
                "ID. Run 'Resolve authors' from the Scholars page to look one "
                "up, then revisit this profile."
            ),
        )

    works, next_cursor = oa.fetch_author_works(key, cursor=cursor, limit=limit)
    if not works and not next_cursor:
        return ScholarWorksResponse(items=[], next_cursor=None)

    matches = _batch_library_match(session, works)
    items: list[ScholarWorkOut] = []
    for w in works:
        wid = oa.work_id(w)
        if not wid:
            continue
        pdf_url, oa_status = oa.work_pdf_url(w)
        is_oa = oa_status == "oa"
        match = matches.get(wid)
        items.append(
            ScholarWorkOut(
                openalex_id=wid,
                title=oa.work_title(w),
                year=_work_year(w),
                venue=oa.work_venue(w),
                doi=oa.work_doi(w),
                arxiv_id=oa.work_arxiv_id(w),
                cited_by_count=w.get("cited_by_count"),
                is_oa=is_oa,
                pdf_url=pdf_url,
                in_library=bool(match and match.in_library),
                library_id=match.id if match else None,
            )
        )
    return ScholarWorksResponse(items=items, next_cursor=next_cursor)
