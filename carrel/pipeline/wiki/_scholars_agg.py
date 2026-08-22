"""Shared scholar aggregation logic.

Authors are stored only as a JSON list on each Paper (``[{name,
openalex_author_id, affiliation}]``); there is no Author table. Both the
``/scholars`` API and the wiki scholar compiler need the same grouping, so the
pure aggregation lives here and the API layer adds its short-TTL response cache
on top.

Authors with an OpenAlex Author ID are grouped by that ID; records without one
fall back to exact-name matching (``key = "name:<name>"``).
"""
from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import cast, String
from sqlmodel import Session, select

from carrel.models import Paper
from carrel.schemas import OpenAlexProfile, ScholarSummary

NAME_KEY_PREFIX = "name:"

# OpenAlex profile cache (24h). Shared by API and compiler to avoid refetches.
_PROFILE_TTL = 24 * 3600.0
_profile_cache: dict[str, tuple[float, OpenAlexProfile | None]] = {}
_profile_lock = threading.Lock()


def author_key(a: dict[str, Any]) -> str | None:
    """Aggregation key for one author record: A-ID when present, else name."""
    a_id = (a.get("openalex_author_id") or "").strip()
    if a_id:
        return a_id
    name = (a.get("name") or "").strip()
    return f"{NAME_KEY_PREFIX}{name}" if name else None


# Backwards-compatible private alias used by the existing API module.
_author_key = author_key


def year_of(p: Paper) -> int | None:
    d = p.publication_date
    return getattr(d, "year", None) if d is not None else None


_year_of = year_of


def aggregate(session: Session) -> list[ScholarSummary]:
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
        year = year_of(p)
        for a in authors:
            if not isinstance(a, dict):
                continue
            key = author_key(a)
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

    out.sort(key=lambda s: (-s.paper_count, -s.total_citations, s.name.lower()))
    return out


_aggregate = aggregate


def matches_author(paper: Paper, key: str) -> bool:
    """True if the paper has an author whose aggregation key equals ``key``."""
    if key.startswith(NAME_KEY_PREFIX):
        wanted = key[len(NAME_KEY_PREFIX):].lower()
        for a in paper.authors or []:
            if isinstance(a, dict) and (a.get("name") or "").strip().lower() == wanted:
                return True
        return False
    for a in paper.authors or []:
        if isinstance(a, dict) and (a.get("openalex_author_id") or "").strip() == key:
            return True
    return False


_matches_author = matches_author


def papers_for_key(session: Session, key: str) -> list[Paper]:
    """In-library papers matching a scholar key, newest first."""
    if key.startswith(NAME_KEY_PREFIX):
        wanted = key[len(NAME_KEY_PREFIX):].lower()
        papers = session.exec(
            select(Paper).where(
                Paper.in_library.is_(True),
                Paper.discarded.is_(False),
                cast(Paper.authors, String).ilike(f"%{wanted}%"),
            )
        ).all()
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
    papers = [p for p in papers if matches_author(p, key)]
    papers.sort(
        key=lambda p: (p.publication_date is not None, p.publication_date),
        reverse=True,
    )
    return papers


def get_profile(key: str) -> OpenAlexProfile | None:
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


_get_profile = get_profile
