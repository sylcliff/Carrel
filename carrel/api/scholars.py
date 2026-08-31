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

``GET /scholars/{key}/works`` pages the **cached** OpenAlex works authored by
this scholar (newest first), joined with the local library so the UI can show
an "In library" badge or an "Import" button next to each work. The cache is
populated lazily on first visit (a background thread kicks off the OpenAlex
cursor walk) and refreshed on demand via
:mod:`carrel.api.scholar_works_sync`. Pagination is offset-based
(``"offset:<N>"`` cursor), not OpenAlex cursor, so repeated page loads after
the first sync are pure local reads.
"""
from __future__ import annotations

from datetime import UTC, datetime

import logging
import re
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from carrel.api._app_cache import cached
from carrel.api._http_cache import (
    apply_etag_headers,
    etag_for_updated_at,
    if_none_match_matches,
    maybe_return_304,
)
from carrel.api.papers import _to_summary
from carrel.cache import openalex_works as cache
from carrel.db import get_session_dep
from carrel.models import AuthorWorksSync, Paper, WikiKind, WikiPage
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

# A-ID → thread that lazy-kicked off the OpenAlex sync. We use this as
# a coarse in-process mutex to avoid two concurrent first-visit
# requests each spawning their own sync; the
# :class:`AuthorWorksSync` table also enforces this server-side, but
# starting only one thread keeps the log quiet.
_lazy_kickoff_threads: dict[str, threading.Thread] = {}
_lazy_kickoff_lock = threading.Lock()

_OFFSET_CURSOR_RE = re.compile(r"^offset:(\d+)$")


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


@cached("scholars_list", tags=("scholars_list", "papers_list"))
def _aggregate_scholars(session: Session) -> list[ScholarSummary]:
    """The underlying aggregation, memoized in the L2 cache.

    The route handler wraps this with the in-process TTL cache
    (:data:`_LIST_TTL`) for hot-path access. The L2 layer is the
    long-lived fan-out target invalidated when papers change.
    """
    return aggregate(session)


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
        items = _aggregate_scholars(session)
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
    request: Request,
    response: Response,
    q: str | None = Query(None, description="Case-insensitive substring match on name"),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session_dep),
) -> list[ScholarSummary]:
    """List scholars ranked by local paper count.

    Layer 1: the underlying aggregation is cached in-process and
    invalidated by ``_library_signature``; the signature is also a
    stable ETag source. ETag includes the filter params so q+limit
    combinations don't share a cache slot.
    """
    sig_ts, sig_count = _library_signature(session)
    etag = etag_for_updated_at(
        None if sig_ts is None else _parse_iso(sig_ts),
        extra=("scholars_list", str(sig_count), q or "", str(limit)),
    )
    if (r := maybe_return_304(request, etag, max_age=30, stale_while_revalidate=60)):
        return r
    if etag is not None:
        apply_etag_headers(response, etag, max_age=30, stale_while_revalidate=60)

    items = _get_scholars(session)
    if q:
        needle = q.strip().lower()
        if needle:
            items = [s for s in items if needle in s.name.lower()]
    return items[:limit]


def _parse_iso(s: str) -> datetime:
    """Parse an ISO timestamp string into a datetime; tolerate trailing Z.

    Module-scope so we don't re-import ``datetime`` on every request.
    Python 3.11+ accepts the ``Z`` suffix natively, so the swap is only
    needed for older string forms; kept for safety.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(UTC)


@router.get("/{key}", response_model=ScholarDetail)
def get_scholar(
    key: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session_dep),
) -> ScholarDetail:
    """One scholar: local papers + OpenAlex profile + compiled wiki page.

    Layer 1: ETag is built from the library signature and the key. The
    OpenAlex profile (h_index etc.) is fetched once and cached in
    :func:`carrel.pipeline.wiki._scholars_agg.get_profile`, so a 304
    here is safe even when the profile exists.
    """
    sig_ts, sig_count = _library_signature(session)
    etag = etag_for_updated_at(
        None if sig_ts is None else _parse_iso(sig_ts),
        extra=("scholar", str(sig_count), key),
    )
    if (r := maybe_return_304(request, etag, max_age=30, stale_while_revalidate=60)):
        return r
    if etag is not None:
        apply_etag_headers(response, etag, max_age=30, stale_while_revalidate=60)

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


def _cache_rows_to_works(
    session: Session, rows: list[AuthorWorksCache]
) -> list[ScholarWorkOut]:
    """Convert cached :class:`AuthorWorksCache` rows to ``ScholarWorkOut``s.

    The ``in_library`` / ``library_id`` join is the one query the cache
    path still needs to issue — the local library state is not in
    ``author_works_cache`` (and shouldn't be, since imports / discards
    would otherwise have to back-write into the cache).
    """
    if not rows:
        return []
    # Build the same identifier set the live path collects, then reuse
    # the existing matcher.
    pseudo: list[dict[str, Any]] = []
    for r in rows:
        pseudo.append(
            {
                "id": r.openalex_id,
                "doi": r.doi,
                # OpenAlex's arXiv id is stored as a bare string (e.g.
                # "2301.12345"); the live path passes the same form.
            }
        )
    # We need arxiv_id too — look it up explicitly because the cached
    # row carries it. Extend the matcher by passing the arxiv ids in.
    oa_ids = {r.openalex_id for r in rows}
    dois = {(r.doi or "").lower() for r in rows if r.doi}
    arxiv_ids = {r.arxiv_id for r in rows if r.arxiv_id}
    conditions = []
    if oa_ids:
        conditions.append(col(Paper.id).in_(oa_ids))
    if dois:
        conditions.append(func.lower(col(Paper.doi)).in_(dois))
    if arxiv_ids:
        conditions.append(col(Paper.arxiv_id).in_(arxiv_ids))
    match_rows: dict[str, Paper] = {}
    if conditions:
        paper_rows = session.exec(
            select(Paper).where(or_(*conditions))
        ).all()
        by_oa = {r.id: r for r in paper_rows if r.id_kind == "openalex"}
        by_doi = {(r.doi or "").lower(): r for r in paper_rows if r.doi}
        by_arxiv = {r.arxiv_id: r for r in paper_rows if r.arxiv_id}
        for r in rows:
            m = (
                by_oa.get(r.openalex_id)
                or by_doi.get((r.doi or "").lower())
                or by_arxiv.get(r.arxiv_id or "")
            )
            if m is not None:
                match_rows[r.openalex_id] = m

    items: list[ScholarWorkOut] = []
    for r in rows:
        m = match_rows.get(r.openalex_id)
        items.append(
            ScholarWorkOut(
                openalex_id=r.openalex_id,
                title=r.title,
                year=r.publication_year,
                venue=r.venue,
                doi=r.doi,
                arxiv_id=r.arxiv_id,
                cited_by_count=r.cited_by_count,
                is_oa=r.is_oa,
                pdf_url=r.pdf_url,
                in_library=bool(m and m.in_library),
                library_id=m.id if m else None,
            )
        )
    return items


def _kickoff_lazy_sync(aid: str) -> None:
    """Spawn a daemon thread that runs the OA cursor walk for ``aid``.

    The thread opens its own session (the request session is closing);
    the in-process ``_lazy_kickoff_threads`` map keeps the GIL from
    spawning duplicates if two requests race. The
    :class:`AuthorWorksSync` table is the authoritative deduplication
    primitive server-side, so the in-process map is just a nicety.
    """
    with _lazy_kickoff_lock:
        existing = _lazy_kickoff_threads.get(aid)
        if existing is not None and existing.is_alive():
            return

        def _runner() -> None:
            from sqlmodel import Session as SqlSession
            from carrel.db import get_app_engine
            from carrel.pipeline.scholar_works_sync import sync_scholar_works

            try:
                engine = get_app_engine()
                with SqlSession(engine) as sess:
                    sync_scholar_works(sess, aid, on_progress=None)
            except Exception:
                logger.exception("lazy scholar_works_sync crashed for %s", aid)
            finally:
                with _lazy_kickoff_lock:
                    _lazy_kickoff_threads.pop(aid, None)

        t = threading.Thread(
            target=_runner, name=f"scholar-sync-{aid}", daemon=True
        )
        _lazy_kickoff_threads[aid] = t
        t.start()


def _parse_offset_cursor(cursor: str | None) -> int:
    """Parse ``"offset:N"`` to ``N``. Any other shape → 0.

    Older sessions might still hold a raw OpenAlex cursor; treat it as
    the start of the list rather than 500ing the page.
    """
    if not cursor:
        return 0
    m = _OFFSET_CURSOR_RE.match(cursor)
    return int(m.group(1)) if m else 0


@router.get("/{key}/works", response_model=ScholarWorksResponse)
def list_scholar_works(
    key: str,
    cursor: str | None = Query(
        None,
        description="Opaque pagination cursor (offset:N) from the previous page",
    ),
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session_dep),
) -> ScholarWorksResponse:
    """OpenAlex works authored by this scholar (newest first), paginated.

    Each item is annotated with ``in_library`` (and ``library_id``) by
    matching against the local Paper table. Only A-ID scholars can be
    resolved through OpenAlex — name-only authors (no A-ID) return 422.

    The endpoint serves the cached :class:`AuthorWorksCache` rows when
    present (``status='ready'`` / ``'stale'``). On first visit, when the
    cache is ``missing`` / ``failed`` / ``loading``, a background thread
    is kicked off to populate it and the response is empty with
    ``status='loading'``; the frontend polls ``GET /scholars/{key}/sync_status``
    until ``ready`` and re-issues this call.

    ``total`` is the cached total for the author (or ``None`` while
    loading) and is the same on every page; the UI uses it to render a
    "Showing X of Y" counter on the section header.
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

    sync_state = session.get(AuthorWorksSync, key)
    sync_status = sync_state.status if sync_state is not None else "missing"

    # First-visit / failure path: kick off a sync, return immediately
    # with an empty page + status='loading'.
    if sync_status in ("missing", "failed"):
        _kickoff_lazy_sync(key)
        return ScholarWorksResponse(
            items=[],
            next_cursor=None,
            total=sync_state.total_count if sync_state else None,
            status="loading",
        )

    if sync_status == "loading":
        # Another worker is already on it — let it finish, return the
        # current best-effort view.
        rows, total = cache.get_cached_works(
            session, key, limit=limit, offset=_parse_offset_cursor(cursor)
        )
        if not rows:
            return ScholarWorksResponse(
                items=[],
                next_cursor=None,
                total=sync_state.total_count,
                status="loading",
            )
        items = _cache_rows_to_works(session, rows)
        next_cursor = (
            f"offset:{_parse_offset_cursor(cursor) + limit}"
            if len(rows) == limit
            else None
        )
        return ScholarWorksResponse(
            items=items,
            next_cursor=next_cursor,
            # Prefer the sync state count (authoritative from OpenAlex) when
            # present — the in-page row count is just "what we already have",
            # which may be smaller mid-sync.
            total=sync_state.total_count or total,
            status="loading",
        )

    # ``ready`` / ``stale``: serve from cache. Zero OA calls.
    offset = _parse_offset_cursor(cursor)
    rows, total = cache.get_cached_works(session, key, limit=limit, offset=offset)
    items = _cache_rows_to_works(session, rows)
    next_cursor = f"offset:{offset + limit}" if len(rows) == limit else None
    return ScholarWorksResponse(
        items=items,
        next_cursor=next_cursor,
        total=sync_state.total_count or total,
        status=sync_status,
    )
