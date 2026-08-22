"""Scholar browse endpoints — aggregate authors across in-library papers.

Authors are stored only as a JSON list on each Paper (``[{name,
openalex_author_id, affiliation}]``); there is no Author table. These routes
aggregate that column at request time (the library is personal-scale). Authors
with an OpenAlex Author ID are grouped by that ID; arXiv/S2 records without
one fall back to exact-name matching (``key = "name:<name>"``).

``GET /scholars`` lists authors ranked by local paper count (the "important"
signal). ``GET /scholars/{key}`` returns the author's in-library papers plus,
when an OpenAlex ID is available, a live OpenAlex profile (works_count,
h_index, ...) fetched on demand and cached in-process.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import cast, String
from sqlmodel import Session, select

from carrel.api.papers import _to_summary
from carrel.db import get_session_dep
from carrel.models import Paper
from carrel.schemas import OpenAlexProfile, ScholarDetail, ScholarSummary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scholars", tags=["scholars"])

NAME_KEY_PREFIX = "name:"

# ---- aggregation cache (list view; invalidated when papers change) --------

_LIST_TTL = 60.0
_list_cache: dict[str, Any] = {"ts": 0.0, "sig": None, "items": []}
_list_lock = threading.Lock()

# ---- OpenAlex profile cache (24h) -----------------------------------------

_PROFILE_TTL = 24 * 3600.0
_profile_cache: dict[str, tuple[float, OpenAlexProfile | None]] = {}
_profile_lock = threading.Lock()


def _author_key(a: dict[str, Any]) -> str | None:
    """Aggregation key for one author record: A-ID when present, else name."""
    a_id = (a.get("openalex_author_id") or "").strip()
    if a_id:
        return a_id
    name = (a.get("name") or "").strip()
    return f"{NAME_KEY_PREFIX}{name}" if name else None


def _year_of(p: Paper) -> int | None:
    d = p.publication_date
    return getattr(d, "year", None) if d is not None else None


def _aggregate(session: Session) -> list[ScholarSummary]:
    """Build every ScholarSummary from in-library papers."""
    papers = session.exec(
        select(Paper).where(Paper.in_library.is_(True), Paper.discarded.is_(False))
    ).all()

    # key -> accumulators
    names: dict[str, Counter] = defaultdict(Counter)  # display-name frequency
    paper_ids: dict[str, set[str]] = defaultdict(set)
    citations: dict[str, int] = defaultdict(int)
    years: dict[str, list[int]] = defaultdict(list)
    # Most recent affiliation per key: track (year, affiliation).
    aff: dict[str, tuple[int, str]] = {}
    has_oa: dict[str, bool] = {}

    for p in papers:
        authors = p.authors
        if not authors:
            continue
        year = _year_of(p)
        for a in authors:
            if not isinstance(a, dict):
                continue
            key = _author_key(a)
            if not key:
                continue
            name = (a.get("name") or "").strip()
            if name:
                names[key][name] += 1
            paper_ids[key].add(p.id)
            citations[key] += p.citation_count or 0
            if year:
                years[key].append(year)
            a_id = (a.get("openalex_author_id") or "").strip()
            if a_id:
                has_oa[key] = True
            affiliation = a.get("affiliation")
            if affiliation and (year is not None or key not in aff):
                if key not in aff or (year is not None and year >= aff[key][0]):
                    aff[key] = (year or 0, affiliation)

    out: list[ScholarSummary] = []
    for key, name_counts in names.items():
        display_name = name_counts.most_common(1)[0][0]
        ys = years.get(key, [])
        out.append(
            ScholarSummary(
                key=key,
                name=display_name,
                affiliation=aff.get(key, (0, None))[1],
                paper_count=len(paper_ids[key]),
                first_year=min(ys) if ys else None,
                last_year=max(ys) if ys else None,
                total_citations=citations.get(key, 0),
                has_openalex=has_oa.get(key, False),
            )
        )

    out.sort(
        key=lambda s: (-s.paper_count, -s.total_citations, s.name.lower())
    )
    return out


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
        items = _aggregate(session)
        _list_cache.update(ts=now, sig=sig, items=items)
        return items


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


def _matches_author(paper: Paper, key: str) -> bool:
    """True if the paper has an author whose aggregation key equals ``key``."""
    if key.startswith(NAME_KEY_PREFIX):
        wanted = key[len(NAME_KEY_PREFIX) :].lower()
        for a in paper.authors or []:
            if isinstance(a, dict) and (a.get("name") or "").strip().lower() == wanted:
                return True
        return False
    for a in paper.authors or []:
        if isinstance(a, dict) and (a.get("openalex_author_id") or "").strip() == key:
            return True
    return False


def _get_profile(key: str) -> OpenAlexProfile | None:
    """Fetch + cache an OpenAlex profile. None for name-only keys or failures."""
    if key.startswith(NAME_KEY_PREFIX):
        return None
    now = time.monotonic()
    with _profile_lock:
        cached = _profile_cache.get(key)
        if cached and now - cached[0] < _PROFILE_TTL:
            return cached[1]
    # Fetch outside the lock (network call); OpenAlex is best-effort.
    from carrel.sources import openalex_client as oa

    raw = oa.fetch_author(key)
    profile = OpenAlexProfile(**raw) if raw else None
    with _profile_lock:
        _profile_cache[key] = (now, profile)
    return profile


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

    if key.startswith(NAME_KEY_PREFIX):
        wanted = key[len(NAME_KEY_PREFIX) :].lower()
        papers = session.exec(
            select(Paper).where(
                Paper.in_library.is_(True),
                Paper.discarded.is_(False),
                cast(Paper.authors, String).ilike(f"%{wanted}%"),
            )
        ).all()
        papers = [p for p in papers if _matches_author(p, key)]
    else:
        # A-ID is a bare token (e.g. A5013214678); substring on the JSON is a
        # reliable prefilter, then exact match in Python.
        papers = session.exec(
            select(Paper).where(
                Paper.in_library.is_(True),
                Paper.discarded.is_(False),
                cast(Paper.authors, String).contains(key),
            )
        ).all()
        papers = [p for p in papers if _matches_author(p, key)]

    papers.sort(
        key=lambda p: (p.publication_date is not None, p.publication_date),
        reverse=True,
    )
    return ScholarDetail(
        scholar=summary,
        papers=[_to_summary(p) for p in papers],
        profile=_get_profile(key),
    )
