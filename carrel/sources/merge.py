"""Merge search hits from multiple metadata sources into one deduplicated list.

The search endpoint fans out to OpenAlex, Semantic Scholar, and arXiv. Each
source returns the same paper under slightly different ids, fields, and
quality signals. This module collapses them:

  * dedup keys tried in order — DOI, arXiv id, S2 paper id, OpenAlex W id,
    then a normalized title as a last resort;
  * field authority on collision:
      - citation_count: max across sources (S2 is freshest but we don't trust
        one source blindly);
      - venue / venue_type: S2's publicationVenue wins, else OA, else arXiv
        (arXiv contributes nothing for venue);
      - authors: first non-empty, preferring OpenAlex (it has ids + affiliation);
      - abstract: first non-empty;
      - pdf_url: arXiv PDF wins (canonical, never a landing page), then OA,
        then S2;
      - tldr: S2 only;
      - ids: union — never drop an id a source contributed.

The merge is pure: no DB, no HTTP, no imports from the rest of carrel beyond
stdlist, so it is trivially unit-testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Source name constants — keep in sync with the SourceKind-ish strings used in
# schemas.SearchResultItem.sources and the frontend source badges.
SOURCE_OPENALEX = "openalex"
SOURCE_SEMANTIC_SCHOLAR = "semantic_scholar"
SOURCE_ARXIV = "arxiv"
SOURCE_LIBRARY = "library"


@dataclass
class MutableSearchHit:
    """A single paper being merged. Field-by-field, not nested, for easy merge."""

    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None
    venue: str | None = None
    venue_type: str | None = None
    publication_date: str | None = None
    citation_count: int | None = None
    tldr: str | None = None
    pdf_url: str | None = None
    snippet: str | None = None

    # Identifier union. Any may be None; at least one must be set (or a title
    # must be present) for the hit to participate in dedup.
    openalex_id: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    s2_id: str | None = None

    sources: set[str] = field(default_factory=set)
    # Per-source rank, filled by the caller for RRF sorting.
    ranks: dict[str, int] = field(default_factory=dict)

    # Library membership, filled in after merge by the endpoint using a batched
    # lookup against the papers table.
    in_library: bool = False
    library_id: str | None = None
    status: str | None = None


# ---------------------------------------------------------------------------
# Adapters — turn a source-specific dict into a MutableSearchHit.
# ---------------------------------------------------------------------------


def from_openalex_work(work: dict[str, Any]) -> MutableSearchHit | None:
    """Build a hit from a raw OpenAlex Work dict (pyalex shape)."""
    # Import lazily: openalex_client configures pyalex at import time and we
    # don't want a circular import at module load.
    from carrel.sources import openalex_client as oa

    title = (work.get("title") or work.get("display_name") or "").strip()
    if not title:
        return None
    oa_id = oa.work_id(work) or None
    doi = _clean_doi(oa.work_doi(work))
    arxiv_id = oa.work_arxiv_id(work)

    abstract = oa.work_abstract(work)
    pdf_url, _oa_status = oa.work_pdf_url(work)

    # venue_type: OpenAlex's primary_location.source.type (journal/repository/...)
    primary = work.get("primary_location") or {}
    src = primary.get("source") or {}
    venue_type = src.get("type") if isinstance(src, dict) else None

    pub_date = work.get("publication_date")
    if not pub_date and work.get("publication_year"):
        pub_date = str(work.get("publication_year"))

    return MutableSearchHit(
        title=title,
        authors=[a.get("name", "") for a in oa.work_authors(work) if a.get("name")],
        abstract=abstract,
        venue=oa.work_venue(work),
        venue_type=venue_type,
        publication_date=pub_date,
        citation_count=work.get("cited_by_count"),
        pdf_url=pdf_url,
        openalex_id=oa_id,
        doi=doi,
        arxiv_id=arxiv_id,
        sources={SOURCE_OPENALEX},
    )


def from_s2_row(row: dict[str, Any]) -> MutableSearchHit | None:
    """Build a hit from a normalized S2 dict (see semanticscholar_client)."""
    title = (row.get("title") or "").strip()
    if not title:
        return None
    return MutableSearchHit(
        title=title,
        authors=list(row.get("authors") or []),
        abstract=row.get("abstract"),
        venue=row.get("venue"),
        venue_type=row.get("venue_type"),
        publication_date=row.get("publication_date"),
        citation_count=row.get("citation_count"),
        tldr=row.get("tldr"),
        pdf_url=row.get("pdf_url"),
        s2_id=row.get("s2_paper_id"),
        doi=_clean_doi(row.get("doi")),
        arxiv_id=_strip_arxiv_version(row.get("arxiv_id")),
        sources={SOURCE_SEMANTIC_SCHOLAR},
    )


def from_arxiv_entry(entry: Any) -> MutableSearchHit | None:
    """Build a hit from an ``ArxivEntry`` (dataclass, but typed Any here)."""
    title = (getattr(entry, "title", "") or "").strip()
    if not title:
        return None
    arxiv_id = _strip_arxiv_version(getattr(entry, "arxiv_id", None))
    updated = getattr(entry, "updated", None)
    pub_date = None
    if updated:
        # arXiv Atom gives ISO-8601 with a trailing Z; take the date portion.
        pub_date = updated[:10] if len(updated) >= 10 else updated
    return MutableSearchHit(
        title=title,
        authors=list(getattr(entry, "authors", []) or []),
        abstract=getattr(entry, "summary", None),
        # arXiv has no venue; leave None so a metadata source can fill it.
        venue=None,
        venue_type="repository",
        publication_date=pub_date,
        # arXiv itself doesn't expose citation counts.
        citation_count=None,
        pdf_url=getattr(entry, "pdf_url", None),
        arxiv_id=arxiv_id,
        sources={SOURCE_ARXIV},
    )


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def merge_search_hits(hits: list[MutableSearchHit]) -> list[MutableSearchHit]:
    """Collide ``hits`` by identifier, merging fields in authority order."""
    # Index keyed by every id we know for an already-seen paper, so a later
    # hit arriving with a *different* id (e.g. S2 row has DOI, OA row has W-id)
    # merges into the same group.
    by_doi: dict[str, MutableSearchHit] = {}
    by_arxiv: dict[str, MutableSearchHit] = {}
    by_s2: dict[str, MutableSearchHit] = {}
    by_oa: dict[str, MutableSearchHit] = {}
    by_title: dict[str, MutableSearchHit] = {}
    merged: list[MutableSearchHit] = []

    for hit in hits:
        # Canonicalize ids in case the caller built the hit directly with
        # unprefixed/uppercase/versioned forms.
        hit.doi = _clean_doi(hit.doi)
        hit.arxiv_id = _strip_arxiv_version(hit.arxiv_id)
        if hit.s2_id:
            hit.s2_id = hit.s2_id.strip()
        if hit.openalex_id:
            hit.openalex_id = hit.openalex_id.strip()
        existing = _find_existing(
            hit, by_doi=by_doi, by_arxiv=by_arxiv, by_s2=by_s2,
            by_oa=by_oa, by_title=by_title,
        )
        if existing is None:
            merged.append(hit)
            _index(hit, by_doi, by_arxiv, by_s2, by_oa, by_title)
            continue
        _merge_into(existing, hit)
        # Re-index in case the incoming hit contributed a new id.
        _index(existing, by_doi, by_arxiv, by_s2, by_oa, by_title)

    return merged


def _has_identifier(hit: MutableSearchHit) -> bool:
    return bool(hit.doi or hit.arxiv_id or hit.s2_id or hit.openalex_id)


def _find_existing(
    hit: MutableSearchHit,
    *,
    by_doi: dict[str, MutableSearchHit],
    by_arxiv: dict[str, MutableSearchHit],
    by_s2: dict[str, MutableSearchHit],
    by_oa: dict[str, MutableSearchHit],
    by_title: dict[str, MutableSearchHit],
) -> MutableSearchHit | None:
    if hit.doi and hit.doi in by_doi:
        return by_doi[hit.doi]
    if hit.arxiv_id and hit.arxiv_id in by_arxiv:
        return by_arxiv[hit.arxiv_id]
    if hit.s2_id and hit.s2_id in by_s2:
        return by_s2[hit.s2_id]
    if hit.openalex_id and hit.openalex_id in by_oa:
        return by_oa[hit.openalex_id]
    # Title-only dedup is a last resort for hits with no ids at all (or for a
    # new id-less hit colliding with an existing one). Don't merge two hits
    # that both have identifiers but disagree — same title ≠ same paper.
    if _has_identifier(hit):
        return None
    title_key = _normalize_title(hit.title)
    if title_key and title_key in by_title:
        return by_title[title_key]
    return None


def _index(
    hit: MutableSearchHit,
    by_doi: dict[str, MutableSearchHit],
    by_arxiv: dict[str, MutableSearchHit],
    by_s2: dict[str, MutableSearchHit],
    by_oa: dict[str, MutableSearchHit],
    by_title: dict[str, MutableSearchHit],
) -> None:
    if hit.doi:
        by_doi[hit.doi] = hit
    if hit.arxiv_id:
        by_arxiv[hit.arxiv_id] = hit
    if hit.s2_id:
        by_s2[hit.s2_id] = hit
    if hit.openalex_id:
        by_oa[hit.openalex_id] = hit
    # Only index titles for hits with no other identifier — otherwise two
    # distinct papers with the same title would collapse together.
    if not _has_identifier(hit):
        title_key = _normalize_title(hit.title)
        if title_key:
            by_title[title_key] = hit


def _merge_into(dst: MutableSearchHit, src: MutableSearchHit) -> None:
    """Merge ``src`` fields into ``dst`` using the authority rules."""
    dst.sources |= src.sources
    # Ranks: per-source, keep the best (smallest) rank. A source may appear in
    # both hits (shouldn't, but be safe) — min keeps the earlier one.
    for k, v in src.ranks.items():
        if k not in dst.ranks or v < dst.ranks[k]:
            dst.ranks[k] = v

    # Identifiers: union. Never drop.
    if src.openalex_id and not dst.openalex_id:
        dst.openalex_id = src.openalex_id
    if src.doi and not dst.doi:
        dst.doi = src.doi
    if src.arxiv_id and not dst.arxiv_id:
        dst.arxiv_id = src.arxiv_id
    if src.s2_id and not dst.s2_id:
        dst.s2_id = src.s2_id

    # citation_count: max.
    if src.citation_count is not None:
        if dst.citation_count is None or src.citation_count > dst.citation_count:
            dst.citation_count = src.citation_count

    # venue / venue_type: S2 > OA > arXiv. We express this as a priority score
    # derived from which sources contributed the value; the highest-priority
    # non-empty value wins.
    dst.venue = _pick_by_priority(
        dst.venue, _source_priority(dst, "venue"),
        src.venue, _source_priority(src, "venue"),
    )
    dst.venue_type = _pick_by_priority(
        dst.venue_type, _source_priority(dst, "venue_type"),
        src.venue_type, _source_priority(src, "venue_type"),
    )

    # authors: prefer OpenAlex (has IDs + affiliations), then S2, then arXiv.
    if src.authors:
        dst_pri = max((_AUTHOR_PRIORITY.get(s, 0) for s in dst.sources if dst.authors), default=0)
        src_pri = max((_AUTHOR_PRIORITY.get(s, 0) for s in src.sources), default=0)
        if not dst.authors or src_pri > dst_pri:
            dst.authors = list(src.authors)

    # abstract: first non-empty.
    if not dst.abstract and src.abstract:
        dst.abstract = src.abstract

    # pdf_url: arXiv > OA > S2.
    dst.pdf_url = _pick_pdf(dst, src)

    # tldr: S2-only, so whichever side has one wins.
    if not dst.tldr and src.tldr:
        dst.tldr = src.tldr

    # publication_date: earliest is usually the arXiv preprint date, but for
    # sorting we prefer the most accurate known date. Prefer a full ISO date
    # over a bare year, then the longer string.
    dst.publication_date = _pick_date(dst.publication_date, src.publication_date)

    # snippet: prefer whichever is set, then longer.
    if src.snippet and (not dst.snippet or len(src.snippet) > len(dst.snippet)):
        dst.snippet = src.snippet


# Venue authority: library (already normalized/imported) > S2 (cleanest
# journal/conference split) > OA > arXiv (no venue). Higher = more authoritative.
_VENUE_PRIORITY = {
    SOURCE_LIBRARY: 4,
    SOURCE_SEMANTIC_SCHOLAR: 3,
    SOURCE_OPENALEX: 2,
    SOURCE_ARXIV: 1,
}

# Author authority: library wins (it's the user's copy, originally normalized
# from OA during import); then OpenAlex (IDs + affiliations), S2, arXiv.
_AUTHOR_PRIORITY = {
    SOURCE_LIBRARY: 4,
    SOURCE_OPENALEX: 3,
    SOURCE_SEMANTIC_SCHOLAR: 2,
    SOURCE_ARXIV: 1,
}

# Venue authority per source is shared with _pick_by_priority. Library wins
# there too for the same reason.


def _source_priority(hit: MutableSearchHit, _field: str) -> int:
    """Highest source-authority present on this hit.

    Both `venue` and `venue_type` use the same ordering today, so `_field` is
    accepted for clarity but ignored. Returns 0 when the hit has no recognized
    source (shouldn't happen).
    """
    best = 0
    for s in hit.sources:
        best = max(best, _VENUE_PRIORITY.get(s, 0))
    return best


def _pick_by_priority(
    dst_val: str | None, dst_pri: int,
    src_val: str | None, src_pri: int,
) -> str | None:
    if not src_val:
        return dst_val
    if not dst_val:
        return src_val
    return src_val if src_pri > dst_pri else dst_val


def _pick_pdf(dst: MutableSearchHit, src: MutableSearchHit) -> str | None:
    """arXiv PDF > OA PDF > S2 PDF. First non-empty in priority order."""
    order = [SOURCE_ARXIV, SOURCE_OPENALEX, SOURCE_SEMANTIC_SCHOLAR]
    candidates = {s: None for s in order}
    # Attribute each side's pdf_url to whichever source it came from. If a hit
    # has multiple sources, we don't know per-field, so attribute to its
    # highest-priority PDF source.
    for hit in (dst, src):
        if not hit.pdf_url:
            continue
        for s in order:
            if s in hit.sources:
                if candidates[s] is None:
                    candidates[s] = hit.pdf_url
                break
    for s in order:
        if candidates[s]:
            return candidates[s]
    return dst.pdf_url or src.pdf_url


def _pick_date(a: str | None, b: str | None) -> str | None:
    """Prefer the more precise date (full ISO > bare year)."""
    if not a:
        return b
    if not b:
        return a
    # Same length → first is fine.
    if len(a) >= len(b):
        return a
    return b


# ---------------------------------------------------------------------------
# Key normalization
# ---------------------------------------------------------------------------

_DOI_PREFIX_RE = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)
_ARXIV_VERSION_RE = re.compile(r"v\d+$")
_TITLE_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _clean_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    d = _DOI_PREFIX_RE.sub("", d)
    # OpenAlex stores DOIs as full URL; pyalex may return them either way.
    return d or None


def _strip_arxiv_version(arxiv_id: str | None) -> str | None:
    if not arxiv_id:
        return None
    a = arxiv_id.strip().lower()
    a = _ARXIV_VERSION_RE.sub("", a)
    # Some sources hand back "arXiv:2301.00001" — strip the prefix.
    if a.startswith("arxiv:"):
        a = a[len("arxiv:"):]
    return a or None


def _normalize_title(title: str | None) -> str:
    if not title:
        return ""
    return _TITLE_STRIP_RE.sub("", title.lower())


# ---------------------------------------------------------------------------
# Ranking helpers
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    hits: list[MutableSearchHit],
    *,
    k: int = 60,
) -> list[MutableSearchHit]:
    """Sort hits by sum of 1/(k + rank) over their source ranks (desc).

    RRF blends per-source relevance rankings without needing score
    normalization across providers. A hit that appears in multiple sources
    ranks higher than one only one source returned. ``k=60`` is the Cormack
    et al. 2009 default.

    Callers should set ``hit.ranks[source] = 1-based position`` per source
    before calling this — usually done in the search endpoint as results come
    back, before merging.
    """
    def score(hit: MutableSearchHit) -> float:
        return sum(1.0 / (k + r) for r in hit.ranks.values()) or 0.0

    return sorted(hits, key=score, reverse=True)
