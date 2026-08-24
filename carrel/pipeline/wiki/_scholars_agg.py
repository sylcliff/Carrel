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

from sqlalchemy import cast, or_, String
from sqlmodel import Session, select

from carrel.models import Paper, ScholarAlias
from carrel.pipeline.wiki._names import normalize_name
from carrel.schemas import OpenAlexProfile, ScholarSummary

NAME_KEY_PREFIX = "name:"

# OpenAlex profile cache (24h). Shared by API and compiler to avoid refetches.
_PROFILE_TTL = 24 * 3600.0
_profile_cache: dict[str, tuple[float, OpenAlexProfile | None]] = {}
_profile_lock = threading.Lock()


def author_key(a: dict[str, Any], session: Session | None = None) -> str | None:
    """Aggregation key for one author record: A-ID when present, else name.

    When ``session`` is supplied, any A-ID is resolved through the
    ``scholar_aliases`` table (see :mod:`carrel.pipeline.scholar_dedup`) so that
    duplicate OpenAlex profiles merge into one scholar. The alias lookup is a
    simple indexed primary-key fetch and is cached for the call's lifetime.
    """
    a_id = (a.get("openalex_author_id") or "").strip()
    if a_id:
        if session is not None:
            a_id = _resolve_with_cache(session, a_id)
        return a_id
    name = (a.get("name") or "").strip()
    if not name:
        return None
    # Normalize so "He Li" / "He-Li" / "he  li" collapse to one key. The
    # display name is unaffected — it comes from the most-common raw form
    # in aggregate()'s Counter.
    return f"{NAME_KEY_PREFIX}{normalize_name(name)}"


# Module-level cache so repeated calls within one aggregation batch don't
# re-query the DB for every author record on every paper. Cleared by
# :func:`invalidate_alias_cache` after a merge/reject so subsequent requests see
# the new mapping.
_alias_cache: dict[str, str] = {}


def _resolve_with_cache(session: Session, aid: str) -> str:
    if aid in _alias_cache:
        return _alias_cache[aid]
    from carrel.pipeline.scholar_dedup import resolve_aid  # noqa: PLC0415
    root = resolve_aid(session, aid)
    _alias_cache[aid] = root
    return root


def invalidate_alias_cache() -> None:
    _alias_cache.clear()


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
            key = author_key(a, session)
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

    # Merge pass: name-only keys whose normalized display name matches a
    # single A-ID key get folded into the A-ID key. This handles the
    # common case where a person has an A-ID in some papers (e.g. those
    # imported from OpenAlex) but not in others (e.g. those imported from
    # Semantic Scholar before authors_backfill resolves the A-IDs). Without
    # this the /scholars list shows two rows for the same person — one with
    # an A-ID, one with the name-only key.
    #
    # Ambiguity guard: if multiple distinct A-ID keys share the same
    # normalized display name, we can't tell which one the name-only row
    # belongs to, so we leave the name-only key in place rather than guess.
    _merge_name_only_into_aid(
        names, paper_ids, citations, years, aff, has_oa
    )

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


def _merge_name_only_into_aid(
    names: dict[str, Counter],
    paper_ids: dict[str, set[str]],
    citations: dict[str, int],
    years: dict[str, list[int]],
    aff: dict[str, tuple[int, str]],
    has_oa: dict[str, bool],
) -> None:
    """Fold ``name:<x>`` keys into the matching A-ID key in-place.

    See :func:`aggregate` for context. A name-only key is merged into an
    A-ID key when the A-ID key's most-common display name normalizes to
    the same string as the name-only key's display name, and exactly one
    A-ID key matches (so the merge can't accidentally conflate two
    different scholars who happen to share a name).
    """
    # Build normalized display name -> A-ID key, only when unambiguous.
    name_to_aid: dict[str, str] = {}
    ambiguous: set[str] = set()
    for key, counts in names.items():
        if key.startswith(NAME_KEY_PREFIX):
            continue
        if not counts:
            continue
        display = counts.most_common(1)[0][0]
        nname = normalize_name(display)
        if not nname:
            continue
        if nname in name_to_aid:
            # Two distinct A-ID keys share the same display name — mark
            # the name as ambiguous so we don't try to merge into either.
            ambiguous.add(nname)
        else:
            name_to_aid[nname] = key
    # Drop ambiguous matches.
    for nname in ambiguous:
        name_to_aid.pop(nname, None)

    # Walk name-only keys once; mutate the underlying dicts so subsequent
    # iterations in the caller see the merged view.
    name_only_keys = [k for k in list(names) if k.startswith(NAME_KEY_PREFIX)]
    for nk in name_only_keys:
        nname = normalize_name(names[nk].most_common(1)[0][0]) if names[nk] else ""
        target = name_to_aid.get(nname) if nname else None
        if not target or target == nk:
            continue
        names[target].update(names[nk])
        paper_ids[target] |= paper_ids[nk]
        citations[target] += citations.get(nk, 0)
        years[target].extend(years.get(nk, []))
        if has_oa.get(nk):
            has_oa[target] = True
        # Keep the most recent affiliation between the two rows.
        if nk in aff:
            old_year, old_aff = aff[nk]
            cur = aff.get(target)
            if not cur or (old_year or 0) >= cur[0]:
                aff[target] = (old_year or 0, old_aff)
        # Drop the name-only key from every accumulator.
        names.pop(nk, None)
        paper_ids.pop(nk, None)
        citations.pop(nk, None)
        years.pop(nk, None)
        aff.pop(nk, None)
        has_oa.pop(nk, None)


_aggregate = aggregate


def matches_author(paper: Paper, key: str, session: Session | None = None) -> bool:
    """True if the paper has an author whose aggregation key equals ``key``.

    For A-ID keys, any of that key's aliases also matches (so a page for the
    canonical A-ID pulls in papers originally tagged with a duplicate A-ID).
    """
    if key.startswith(NAME_KEY_PREFIX):
        wanted = key[len(NAME_KEY_PREFIX):].lower()
        for a in paper.authors or []:
            if isinstance(a, dict) and (a.get("name") or "").strip().lower() == wanted:
                return True
        return False
    wanted_ids = {key}
    if session is not None:
        wanted_ids.update(_aliases_of(session, key))
    for a in paper.authors or []:
        if isinstance(a, dict):
            aid = (a.get("openalex_author_id") or "").strip()
            if aid and aid in wanted_ids:
                return True
    return False


def _aliases_of(session: Session, canonical_aid: str) -> set[str]:
    """All A-IDs (including the canonical itself) that resolve to it."""
    # Walk the chain in both directions: alias rows point alias->canonical, so
    # find rows whose canonical_aid matches AND rows whose alias eventually
    # resolves here. `resolve_aid` gives the latter.
    out = {canonical_aid}
    for r in session.exec(
        select(ScholarAlias).where(ScholarAlias.canonical_aid == canonical_aid)
    ).all():
        if r.source != "reject":
            out.add(r.alias_aid)
    return out


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
        # reliable prefilter. Pull rows matching the canonical key OR any alias,
        # then confirm with matches_author (which knows the alias set).
        wanted_ids = [key, *sorted(_aliases_of(session, key) - {key})]
        conditions = [cast(Paper.authors, String).contains(k) for k in wanted_ids]
        stmt = select(Paper).where(
            Paper.in_library.is_(True),
            Paper.discarded.is_(False),
        )
        if conditions:
            stmt = stmt.where(or_(*conditions))
        papers = session.exec(stmt).all()
    papers = [p for p in papers if matches_author(p, key, session)]
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


def enumerate_entities(session: Session) -> list[Any]:
    """Return an :class:`EntityRef` per live scholar for the wiki reconciler.

    Lightweight wrapper over :func:`aggregate` — emits one
    ``EntityRef(kind='scholar', entity_key=..., slug=..., title=..., extra=...)``
    per aggregation key.  ``entity_key`` matches the wiki catalog's
    convention: ``scholar:<A-ID>`` for A-ID authors, ``scholar:name:<normalized>``
    for name-only authors.  ``extra['scholar_aid']`` is set when known so
    the reconciler can populate ``WikiPage.scholar_aid`` on freshly opened rows.
    """
    # Imported lazily to avoid a circular import — _entities.py depends on
    # this module at function-call time, not at import time.
    from carrel.pipeline.wiki._entities import EntityRef
    from carrel.pipeline.wiki._slug import scholar_slug

    out: list[EntityRef] = []
    for s in aggregate(session):
        if s.key.startswith(NAME_KEY_PREFIX):
            entity_key = f"scholar:name:{s.key[len(NAME_KEY_PREFIX):]}"
            extra: dict[str, Any] = {}
        else:
            entity_key = f"scholar:{s.key}"
            extra = {"scholar_aid": s.key}
        out.append(
            EntityRef(
                entity_key=entity_key,
                kind="scholar",
                slug=scholar_slug(
                    s.key if not s.key.startswith(NAME_KEY_PREFIX) else None,
                    s.name,
                ),
                title=s.name,
                extra=extra,
            )
        )
    return out
