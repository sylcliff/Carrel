"""Search endpoints.

Three sources are fanned out in parallel and merged into one result list:

  * **library** — SQL ILIKE on title/abstract/authors/identifiers of the
    ``papers`` table.
  * **OpenAlex** — faceted Works search with inverted-index abstract
    reconstruction and Zenodo filtering.
  * **Semantic Scholar** — Graph API search (relevance or citation/date sort
    via the bulk endpoint), contributing citation counts, venue type, and TLDR.
  * **arXiv** — Atom API relevance search, contributing the canonical PDF link
    and the freshest preprint metadata.

Results are deduplicated by DOI → arXiv id → S2 id → OpenAlex id → normalized
title, merged with field-authority rules in :mod:`carrel.sources.merge`, and
ordered by reciprocal-rank fusion (or by citations / date when requested).
Per-source failures are captured in ``warnings`` rather than aborting the
response. Full-text semantic search over embedded chunks is kept as a separate
endpoint.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, cast
from sqlmodel import Session, col, or_, select

from carrel import embeddings as emb
from carrel import spelling
from carrel.db import get_session_dep
from carrel.models import Chunk, Paper, PaperStatus, SourceKind
from carrel.schemas import (
    ImportPaperIn,
    ImportPaperOut,
    SearchResponse,
    SearchResultIds,
    SearchResultItem,
    SemanticSearchHit,
    SemanticSearchResponse,
    SemanticSearchResult,
)
from carrel.sources import arxiv as arxiv_src
from carrel.sources import merge as merge_mod
from carrel.sources import openalex_client as oa
from carrel.sources import semanticscholar_client as s2
from carrel.sources.normalize import is_zenodo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

# Seeded once per process. Lazy — first search pays the cost (~100ms for a
# few thousand papers) so startup stays fast.
_seeded = False
_seed_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Spelling
# ---------------------------------------------------------------------------


def _ensure_spelling_seeded(session: Session) -> None:
    """Load library jargon into the spell checker on first search."""
    global _seeded
    if _seeded:
        return
    with _seed_lock:
        if _seeded:
            return
        try:
            spelling.seed_from_library(session)
        except Exception:  # noqa: BLE001 - spelling is best-effort
            logger.warning("failed to seed spell corrector from library", exc_info=True)
        _seeded = True


def _apply_correction(q: str, session: Session) -> tuple[str, str | None]:
    """Returns (query_to_search, original_query_if_corrected)."""
    _ensure_spelling_seeded(session)
    try:
        return spelling.correct_query(q)
    except Exception:  # noqa: BLE001
        logger.warning("spell correction failed for %r", q, exc_info=True)
        return q, None


# ---------------------------------------------------------------------------
# Snippet helpers
# ---------------------------------------------------------------------------


def _abstract_excerpt(text: str | None, query: str, *, width: int = 240) -> str | None:
    if not text:
        return None
    if not query:
        return (text[:width] + "…") if len(text) > width else text
    lower = text.lower()
    idx = lower.find(query.lower())
    if idx < 0:
        return (text[:width] + "…") if len(text) > width else text
    half = width // 2
    start = max(0, idx - half)
    end = min(len(text), start + width)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _author_names(paper: Paper) -> list[str]:
    return [a.get("name", "") for a in (paper.authors or []) if a.get("name")]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchFilters:
    year_from: int | None = None
    year_to: int | None = None
    min_citations: int | None = None
    open_access_only: bool = False
    sort: str = "relevance"  # "relevance" | "citations" | "date"
    sources: tuple[str, ...] = ()  # empty = all enabled


_ALLOWED_SORTS = {"relevance", "citations", "date"}
_ALLOWED_SOURCES = {
    merge_mod.SOURCE_OPENALEX,
    merge_mod.SOURCE_SEMANTIC_SCHOLAR,
    merge_mod.SOURCE_ARXIV,
}


# ---------------------------------------------------------------------------
# Local search
# ---------------------------------------------------------------------------


def _local_search_items(
    session: Session, q: str, limit: int
) -> list[merge_mod.MutableSearchHit]:
    """SQL ILIKE search, returning MutableSearchHits tagged ``library``."""
    pattern = f"%{q}%"
    stmt = (
        select(Paper)
        .where(
            Paper.in_library.is_(True),
            or_(
                col(Paper.title).ilike(pattern),
                col(Paper.abstract).ilike(pattern),
                cast(Paper.authors, String).ilike(pattern),
                col(Paper.doi).ilike(pattern),
                col(Paper.arxiv_id).ilike(pattern),
            ),
        )
        .order_by(Paper.updated_at.desc())
        .limit(limit)
    )
    rows = session.exec(stmt).all()
    out: list[merge_mod.MutableSearchHit] = []
    for p in rows:
        snippet = _abstract_excerpt(p.abstract, q)
        hit = merge_mod.MutableSearchHit(
            title=p.title or "(untitled)",
            authors=_author_names(p),
            abstract=p.abstract,
            venue=p.venue,
            publication_date=p.publication_date.isoformat() if p.publication_date else None,
            citation_count=p.citation_count,
            pdf_url=p.pdf_url,
            snippet=snippet,
            openalex_id=p.id if p.id_kind == "openalex" else None,
            doi=(p.doi or "").lower().removeprefix("https://doi.org/") if p.doi else None,
            arxiv_id=p.arxiv_id,
            s2_id=p.s2_paper_id,
            sources={merge_mod.SOURCE_LIBRARY},
            in_library=True,
            library_id=p.id,
            status=p.status,
        )
        out.append(hit)
    return out


# ---------------------------------------------------------------------------
# External fan-out
# ---------------------------------------------------------------------------


def _search_openalex(
    q: str, filters: SearchFilters, limit: int
) -> list[merge_mod.MutableSearchHit]:
    works = oa.search_work(
        q,
        limit=limit,
        year_from=filters.year_from,
        year_to=filters.year_to,
        min_citations=filters.min_citations,
        open_access_only=filters.open_access_only,
        sort=filters.sort,
    )
    out: list[merge_mod.MutableSearchHit] = []
    for i, w in enumerate(works):
        if is_zenodo(oa.work_doi(w), oa.work_venue(w)):
            continue
        hit = merge_mod.from_openalex_work(w)
        if hit is None:
            continue
        hit.ranks[merge_mod.SOURCE_OPENALEX] = i + 1
        hit.snippet = _abstract_excerpt(hit.abstract, q)
        out.append(hit)
    return out


def _search_s2(
    q: str, filters: SearchFilters, limit: int
) -> list[merge_mod.MutableSearchHit]:
    rows = s2.search_papers(
        q,
        limit=limit,
        year_from=filters.year_from,
        year_to=filters.year_to,
        min_citations=filters.min_citations,
        open_access_only=filters.open_access_only,
        # Default sort is relevance; "citations"/"date" trigger the bulk
        # endpoint inside the client. We don't pass venue_types here because
        # the filter bar doesn't expose them yet.
        sort=filters.sort,
    )
    out: list[merge_mod.MutableSearchHit] = []
    for i, row in enumerate(rows):
        hit = merge_mod.from_s2_row(row)
        if hit is None:
            continue
        hit.ranks[merge_mod.SOURCE_SEMANTIC_SCHOLAR] = i + 1
        hit.snippet = _abstract_excerpt(hit.abstract, q)
        out.append(hit)
    return out


def _search_arxiv(
    q: str, filters: SearchFilters, limit: int
) -> list[merge_mod.MutableSearchHit]:
    entries = arxiv_src.search(q, limit=limit)
    out: list[merge_mod.MutableSearchHit] = []
    for i, e in enumerate(entries):
        hit = merge_mod.from_arxiv_entry(e)
        if hit is None:
            continue
        # arXiv Atom API doesn't support year/citation/OA filters server-side;
        # post-filter by year. arXiv is always OA, so open_access_only is a
        # no-op. Citation filter can't be applied without a second source.
        if filters.year_from and hit.publication_date:
            try:
                year = int(hit.publication_date[:4])
            except ValueError:
                year = None
            if year is not None and year < filters.year_from:
                continue
        if filters.year_to and hit.publication_date:
            try:
                year = int(hit.publication_date[:4])
            except ValueError:
                year = None
            if year is not None and year > filters.year_to:
                continue
        hit.ranks[merge_mod.SOURCE_ARXIV] = i + 1
        hit.snippet = _abstract_excerpt(hit.abstract, q)
        out.append(hit)
    return out


def _multi_source_search(
    q: str, filters: SearchFilters, per_source_limit: int
) -> tuple[list[merge_mod.MutableSearchHit], list[str]]:
    """Fan out to the enabled external sources and merge results."""
    enabled = set(filters.sources) if filters.sources else set(_ALLOWED_SOURCES)
    enabled &= _ALLOWED_SOURCES

    jobs: dict[str, Any] = {}
    if merge_mod.SOURCE_OPENALEX in enabled:
        jobs[merge_mod.SOURCE_OPENALEX] = (
            _search_openalex,
            (q, filters, per_source_limit),
        )
    if merge_mod.SOURCE_SEMANTIC_SCHOLAR in enabled:
        jobs[merge_mod.SOURCE_SEMANTIC_SCHOLAR] = (
            _search_s2,
            (q, filters, per_source_limit),
        )
    if merge_mod.SOURCE_ARXIV in enabled:
        jobs[merge_mod.SOURCE_ARXIV] = (
            _search_arxiv,
            (q, filters, per_source_limit),
        )

    results: dict[str, list[merge_mod.MutableSearchHit]] = {}
    warnings: list[str] = []

    # Run the HTTP calls in parallel. SQLite session isn't used inside these
    # workers — library membership is resolved after the fan-out.
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = {
            name: pool.submit(fn, *args) for name, (fn, args) in jobs.items()
        }
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except Exception as e:  # noqa: BLE001 - per-source soft failure
                logger.warning("search source %s failed for %r: %s", name, q, e)
                warnings.append(f"{name}: {e}")
                results[name] = []

    merged = merge_mod.merge_search_hits(
        [hit for hits in results.values() for hit in hits]
    )

    if filters.sort == "relevance":
        merged = merge_mod.reciprocal_rank_fusion(merged)
    elif filters.sort == "citations":
        merged.sort(
            key=lambda h: (h.citation_count if h.citation_count is not None else -1),
            reverse=True,
        )
    elif filters.sort == "date":
        merged.sort(key=lambda h: h.publication_date or "", reverse=True)

    return merged, warnings


# ---------------------------------------------------------------------------
# Batched library-membership lookup
# ---------------------------------------------------------------------------


def _resolve_library_membership(
    session: Session, hits: list[merge_mod.MutableSearchHit]
) -> None:
    """Set ``in_library`` / ``library_id`` / ``status`` on each hit.

    One batched SELECT across doi / arxiv_id / s2_paper_id / (W-)id, following
    the same pattern as carrel.api.citations._resolve_library so we don't run
    three queries per result.
    """
    if not hits:
        return

    dois: set[str] = set()
    arxiv_ids: set[str] = set()
    s2_ids: set[str] = set()
    oa_ids: set[str] = set()
    for h in hits:
        if h.doi:
            dois.add(h.doi.lower())
        if h.arxiv_id:
            arxiv_ids.add(h.arxiv_id.lower())
        if h.s2_id:
            s2_ids.add(h.s2_id)
        if h.openalex_id and h.openalex_id.startswith("W"):
            oa_ids.add(h.openalex_id)

    clauses = []
    if dois:
        doi_variants = list(dois) + [f"https://doi.org/{d}" for d in dois]
        clauses.append(Paper.doi.in_(doi_variants))
    if arxiv_ids:
        clauses.append(Paper.arxiv_id.in_(list(arxiv_ids)))
    if s2_ids:
        clauses.append(Paper.s2_paper_id.in_(list(s2_ids)))
    if oa_ids:
        oa_variants = list(oa_ids) + [f"https://openalex.org/{o}" for o in oa_ids]
        clauses.append(Paper.id.in_(oa_variants))

    if not clauses:
        return

    papers = session.exec(select(Paper).where(or_(*clauses))).all()

    def _match(hit: merge_mod.MutableSearchHit) -> Paper | None:
        for p in papers:
            # Only papers actually in the library count as "in library" here.
            # A sync-discovered inbox row matching by identifier should remain
            # importable from Search (it is not the user's library yet).
            if not p.in_library:
                continue
            if hit.openalex_id and (p.id == hit.openalex_id or p.id == f"https://openalex.org/{hit.openalex_id}"):
                return p
            if hit.doi and p.doi:
                bare = p.doi.lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
                if bare == hit.doi.lower():
                    return p
            if hit.arxiv_id and p.arxiv_id:
                if p.arxiv_id.lower() == hit.arxiv_id.lower():
                    return p
            if hit.s2_id and p.s2_paper_id == hit.s2_id:
                return p
        return None

    for hit in hits:
        if hit.in_library:
            continue
        m = _match(hit)
        if m is not None:
            hit.in_library = True
            hit.library_id = m.id
            hit.status = m.status
            # Tag "library" so the source badge/filter recognizes it — this
            # runs for external hits that turn out to be in the user's library.
            # (Hits that originated from _local_search_items already carry it.)
            hit.sources.add(merge_mod.SOURCE_LIBRARY)
            # A library paper's local citation count can be fresher than what
            # the external sources returned.
            if m.citation_count is not None and (
                hit.citation_count is None or m.citation_count > hit.citation_count
            ):
                hit.citation_count = m.citation_count


# ---------------------------------------------------------------------------
# Serialize MutableSearchHit -> SearchResultItem
# ---------------------------------------------------------------------------


def _to_item(hit: merge_mod.MutableSearchHit) -> SearchResultItem:
    return SearchResultItem(
        title=hit.title,
        authors=list(hit.authors or []),
        abstract=hit.abstract,
        venue=hit.venue,
        venue_type=hit.venue_type,
        publication_date=hit.publication_date,
        citation_count=hit.citation_count,
        tldr=hit.tldr,
        pdf_url=hit.pdf_url,
        snippet=hit.snippet,
        ids=SearchResultIds(
            openalex=hit.openalex_id,
            doi=hit.doi,
            arxiv=hit.arxiv_id,
            s2=hit.s2_id,
        ),
        sources=sorted(hit.sources),
        in_library=hit.in_library,
        library_id=hit.library_id,
        status=hit.status,
    )


def _parse_filters(
    *,
    year_from: int | None,
    year_to: int | None,
    min_citations: int | None,
    open_access_only: bool,
    sort: str,
    sources: list[str] | None,
) -> SearchFilters:
    sort = sort if sort in _ALLOWED_SORTS else "relevance"
    src_tuple = tuple(s for s in (sources or []) if s in _ALLOWED_SOURCES)
    return SearchFilters(
        year_from=year_from,
        year_to=year_to,
        min_citations=min_citations,
        open_access_only=open_access_only,
        sort=sort,
        sources=src_tuple,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/search/local", response_model=list[SearchResultItem])
def search_local(
    q: str = Query("", min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    correct: bool = Query(True),
    session: Session = Depends(get_session_dep),
) -> list[SearchResultItem]:
    q = q.strip()
    if not q:
        return []
    if correct:
        q, _ = _apply_correction(q, session)
    hits = _local_search_items(session, q, limit)
    return [_to_item(h) for h in hits]


@router.get("/search/external", response_model=list[SearchResultItem])
def search_external(
    q: str = Query("", min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=50),
    year_from: int | None = Query(None, ge=1900, le=2100),
    year_to: int | None = Query(None, ge=1900, le=2100),
    min_citations: int | None = Query(None, ge=0),
    open_access_only: bool = Query(False),
    sort: str = Query("relevance"),
    sources: list[str] | None = Query(None),
    correct: bool = Query(True),
    session: Session = Depends(get_session_dep),
) -> list[SearchResultItem]:
    q = q.strip()
    if not q:
        return []
    if correct:
        q, _ = _apply_correction(q, session)
    filters = _parse_filters(
        year_from=year_from, year_to=year_to,
        min_citations=min_citations, open_access_only=open_access_only,
        sort=sort, sources=sources,
    )
    hits, _warnings = _multi_source_search(q, filters, per_source_limit=limit)
    _resolve_library_membership(session, hits)
    return [_to_item(h) for h in hits[:limit]]


@router.get("/search", response_model=SearchResponse)
def search_combined(
    q: str = Query("", min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=50),
    year_from: int | None = Query(None, ge=1900, le=2100),
    year_to: int | None = Query(None, ge=1900, le=2100),
    min_citations: int | None = Query(None, ge=0),
    open_access_only: bool = Query(False),
    sort: str = Query("relevance"),
    sources: list[str] | None = Query(None),
    correct: bool = Query(True),
    include_local: bool = Query(True),
    session: Session = Depends(get_session_dep),
) -> SearchResponse:
    """One-shot search: external sources merged with library results."""
    q = q.strip()
    if not q:
        return SearchResponse(query="", results=[], warnings=[])
    if correct:
        corrected, original = _apply_correction(q, session)
    else:
        corrected, original = q, None

    filters = _parse_filters(
        year_from=year_from, year_to=year_to,
        min_citations=min_citations, open_access_only=open_access_only,
        sort=sort, sources=sources,
    )

    # External fan-out first (HTTP-bound) — can run while local query runs.
    external_hits, warnings = _multi_source_search(
        corrected, filters, per_source_limit=limit,
    )

    local_hits: list[merge_mod.MutableSearchHit] = []
    if include_local:
        local_hits = _local_search_items(session, corrected, limit)

    # Merge local + external together so a paper that's both in library and
    # returned by an external source becomes one row tagged "library".
    merged = merge_mod.merge_search_hits(local_hits + external_hits)

    if filters.sort == "relevance":
        # Local hits don't have an external rank, so blend: give library hits
        # a strong head start (rank 1) since the user already owns them.
        for h in merged:
            if merge_mod.SOURCE_LIBRARY in h.sources and not h.ranks:
                h.ranks[merge_mod.SOURCE_LIBRARY] = 1
        merged = merge_mod.reciprocal_rank_fusion(merged)
    elif filters.sort == "citations":
        merged.sort(
            key=lambda h: (h.citation_count if h.citation_count is not None else -1),
            reverse=True,
        )
    elif filters.sort == "date":
        merged.sort(key=lambda h: h.publication_date or "", reverse=True)

    # Library membership on the external-only rows. Local hits already have
    # in_library=True; _resolve_library_membership skips them.
    _resolve_library_membership(session, merged)

    return SearchResponse(
        query=corrected,
        corrected_from=original,
        results=[_to_item(h) for h in merged[:limit]],
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Full-text semantic search (unchanged)
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _decode_embedding(raw) -> list[float] | None:
    """SQLite stores the list as a JSON string; pgvector returns a list. Handle both."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return list(v) if isinstance(v, list) else None
        except (ValueError, TypeError):
            return None
    return None


def _semantic_search_sqlite(
    session: Session, q_vec: list[float], limit: int
) -> list[tuple[str, int, str | None, str, float]]:
    """In-memory cosine scan over chunks (SQLite test fallback)."""
    rows = session.exec(select(Chunk)).all()
    scored: list[tuple[str, int, str | None, str, float]] = []
    for r in rows:
        vec = _decode_embedding(r.embedding)
        if not vec:
            continue
        score = _cosine(q_vec, vec)
        scored.append((r.paper_id, r.chunk_index, r.heading, r.content_md, score))
    scored.sort(key=lambda x: x[4], reverse=True)
    return scored[:limit]


def _semantic_search_postgres(
    session: Session, q_vec: list[float], limit: int
) -> list[tuple[str, int, str | None, str, float]]:
    """pgvector cosine distance. Distance d → similarity 1 - d."""
    distance = Chunk.embedding.cosine_distance(q_vec)  # type: ignore[attr-defined]
    stmt = (
        select(
            Chunk.paper_id,
            Chunk.chunk_index,
            Chunk.heading,
            Chunk.content_md,
            distance.label("distance"),
        )
        .order_by("distance")
        .limit(limit)
    )
    rows = session.exec(stmt).all()
    out: list[tuple[str, int, str | None, str, float]] = []
    for pid, idx, heading, body, dist in rows:
        out.append((pid, idx, heading, body, 1.0 - float(dist)))
    return out


def _semantic_search(
    session: Session, q: str, limit: int
) -> list[SemanticSearchResult]:
    """Run a full-text query and group chunk hits by paper."""
    from carrel.main import app_config  # set in lifespan

    model = app_config.embeddings.model
    try:
        q_vecs = emb.embed_texts([q], model=model, batch_size=1)
    except Exception as e:  # noqa: BLE001
        logger.warning("semantic search: embedding query failed: %s", e)
        return []
    if not q_vecs:
        return []
    q_vec = q_vecs[0]

    raw_limit = min(limit * 3, 100)
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        hits = _semantic_search_postgres(session, q_vec, raw_limit)
    else:
        hits = _semantic_search_sqlite(session, q_vec, raw_limit)

    by_paper: dict[str, list[tuple[int, str | None, str, float]]] = defaultdict(list)
    for pid, idx, heading, body, score in hits:
        by_paper[pid].append((idx, heading, body, score))
    paper_ids = list(by_paper.keys())

    paper_rows: dict[str, Paper] = {}
    if paper_ids:
        for p in session.exec(select(Paper).where(col(Paper.id).in_(paper_ids))).all():
            paper_rows[p.id] = p

    results: list[SemanticSearchResult] = []
    for pid, chunk_hits in by_paper.items():
        paper = paper_rows.get(pid)
        if paper is None:
            continue
        chunk_hits.sort(key=lambda x: x[3], reverse=True)
        top = chunk_hits[:3]
        results.append(SemanticSearchResult(
            id=paper.id,
            title=paper.title,
            venue=paper.venue,
            publication_date=paper.publication_date.isoformat() if paper.publication_date else None,
            authors=_author_names(paper),
            doi=paper.doi,
            arxiv_id=paper.arxiv_id,
            status=paper.status,
            best_score=top[0][3],
            hits=[
                SemanticSearchHit(
                    paper_id=pid,
                    chunk_index=idx,
                    heading=heading,
                    snippet=_excerpt(body, q),
                    score=score,
                )
                for idx, heading, body, score in top
            ],
        ))
    results.sort(key=lambda r: r.best_score, reverse=True)
    return results[:limit]


def _excerpt(text: str, q: str, *, width: int = 280) -> str:
    if not text:
        return ""
    if not q:
        return text[:width] + ("…" if len(text) > width else "")
    lower = text.lower()
    needle = q.lower()
    idx = lower.find(needle)
    if idx < 0:
        return text[:width] + ("…" if len(text) > width else "")
    half = width // 2
    start = max(0, idx - half)
    end = min(len(text), start + width)
    snip = text[start:end]
    if start > 0:
        snip = "…" + snip
    if end < len(text):
        snip = snip + "…"
    return snip


@router.get("/search/semantic", response_model=SemanticSearchResponse)
def search_semantic(
    q: str = Query("", min_length=0, max_length=500),
    limit: int = Query(10, ge=1, le=30),
    correct: bool = Query(True),
    session: Session = Depends(get_session_dep),
) -> SemanticSearchResponse:
    """Full-text semantic search over embedded chunks."""
    q = q.strip()
    if not q:
        return SemanticSearchResponse(query="", results=[])
    if correct:
        corrected, original = _apply_correction(q, session)
    else:
        corrected, original = q, None
    return SemanticSearchResponse(
        query=corrected,
        corrected_from=original,
        results=_semantic_search(session, corrected, limit),
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _resolve_work_for_import(
    *,
    oa_id: str | None,
    doi: str | None,
    arxiv_id: str | None,
    s2_id: str | None,
    title: str | None = None,
) -> tuple[dict, str] | None:
    """Resolve whichever identifier the client gave us to importable metadata.

    Returns ``(record, source)`` where ``source`` is ``"openalex"`` (a real
    OpenAlex Work dict) or ``"semantic_scholar"`` (a synthetic record built
    from an S2 paper, used when OpenAlex has no copy). Returns None if nothing
    resolves on either source.

    Resolution order: OpenAlex W-id → DOI → arXiv id (with title-hint fallback)
    → S2 id (mapped to DOI/arXiv, then OpenAlex title search). If every
    OpenAlex path fails but S2 has the paper, fall back to the S2 record so the
    paper can still be imported (id_kind=semanticscholar).
    """
    work: dict | None = None
    if oa_id:
        oa_id = oa_id.rsplit("/", 1)[-1]
        try:
            w = oa.Works()[oa_id]
            work = dict(w) if w else None
        except Exception as e:  # noqa: BLE001
            logger.warning("openalex lookup W=%s failed: %s", oa_id, e)
            work = None
    if work is None and doi:
        work = oa.lookup_by_doi(doi)
    if work is None and arxiv_id:
        work = oa.lookup_by_arxiv_id(arxiv_id, title_hint=title)

    # We need an S2 lookup only if OpenAlex hasn't resolved the work: either
    # the client gave an s2 id directly, or all OA paths (W/DOI/arXiv) missed.
    s2_paper: dict | None = None
    if work is None:
        # S2's /paper/{id} needs a prefixed id for DOI/arXiv lookups.
        if s2_id:
            lookup_id = s2_id
        elif doi:
            lookup_id = f"DOI:{doi}"
        elif arxiv_id:
            lookup_id = f"ARXIV:{arxiv_id}"
        else:
            lookup_id = None
        if lookup_id:
            try:
                s2_paper = s2.fetch_paper(lookup_id)
            except s2.S2Error as e:
                logger.warning("S2 lookup for import %s failed: %s", lookup_id, e)
                s2_paper = None
            if s2_paper:
                doi = doi or s2_paper.get("doi")
                arxiv_id = arxiv_id or s2_paper.get("arxiv_id")
                title = title or s2_paper.get("title")

    if work is None and s2_paper:
        if s2_paper.get("doi"):
            work = oa.lookup_by_doi(s2_paper["doi"])
        if work is None and s2_paper.get("arxiv_id"):
            work = oa.lookup_by_arxiv_id(
                s2_paper["arxiv_id"], title_hint=s2_paper.get("title")
            )
    if work is None and s2_paper and s2_paper.get("title"):
        # Last-ditch OpenAlex path: title search with a strict match threshold.
        try:
            cand = oa.search_work(s2_paper["title"], limit=3)
        except Exception:  # noqa: BLE001
            cand = []
        for w in cand:
            wt = (w.get("title") or w.get("display_name") or "").lower()
            if _title_similarity(s2_paper["title"], wt) >= 0.85:
                work = w
                break

    if work is not None:
        return work, "openalex"

    # OpenAlex has nothing. If S2 found the paper, build a synthetic record so
    # it can still be imported without a canonical Work ID.
    if s2_paper:
        return _s2_record_to_work(s2_paper), "semantic_scholar"

    return None


def _s2_record_to_work(rec: dict[str, Any]) -> dict[str, Any]:
    """Adapt a normalized S2 search row into a Work-shaped dict for import.

    Only the fields the import path reads are populated. Marked with
    ``_source == "semantic_scholar"`` so :func:`import_external_paper` can
    branch on it.
    """
    authors = [
        {"name": name}
        for name in (rec.get("authors") or [])
        if isinstance(name, str) and name.strip()
    ]
    return {
        "_source": "semantic_scholar",
        "id": f"https://www.semanticscholar.org/paper/{rec.get('s2_paper_id')}",
        "s2_paper_id": rec.get("s2_paper_id"),
        "title": rec.get("title") or "(untitled)",
        "doi": rec.get("doi"),
        "arxiv_id": rec.get("arxiv_id"),
        "venue": rec.get("venue"),
        "publication_date": rec.get("publication_date"),
        "abstract": rec.get("abstract"),
        "authors": authors,
        "pdf_url": rec.get("pdf_url"),
        "citation_count": rec.get("citation_count"),
        "reference_count": rec.get("reference_count"),
        "raw": rec,
    }


def _title_similarity(a: str, b: str) -> float:
    """Token-overlap ratio in [0, 1] for loose title matching."""
    ta = {t for t in _TITLE_TOKEN_RE.findall(a.lower()) if len(t) > 1}
    tb = {t for t in _TITLE_TOKEN_RE.findall(b.lower()) if len(t) > 1}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta | tb), 1)


_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")


@router.post("/import", response_model=ImportPaperOut)
def import_external_paper(
    body: ImportPaperIn,
    session: Session = Depends(get_session_dep),
) -> ImportPaperOut:
    """Fetch an external work and upsert it into the library.

    Accepts any of ``openalex_id`` / ``doi`` / ``arxiv_id`` / ``s2``.
    """
    resolved = _resolve_work_for_import(
        oa_id=body.openalex_id,
        doi=body.doi,
        arxiv_id=body.arxiv_id,
        s2_id=body.s2,
        title=body.title,
    )
    if not resolved:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=404,
            detail="Could not find this paper on OpenAlex or Semantic Scholar.",
        )
    work, source = resolved

    # Block Zenodo deposits (same filter as subscription sync).
    if is_zenodo(oa.work_doi(work), oa.work_venue(work)):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail="Zenodo deposits are not papers and cannot be imported into the library",
        )

    s2_id = work.get("s2_paper_id") if source == "semantic_scholar" else None
    existing = _find_library_match(
        session,
        oa_id=oa.work_id(work) if source == "openalex" else None,
        doi=oa.work_doi(work),
        arxiv_id=oa.work_arxiv_id(work),
        s2_id=s2_id,
    )
    if existing is not None:
        # The paper already exists (library or sync-discovered inbox). Importing
        # from Search/citations moves it into the library if it wasn't already.
        if not existing.in_library:
            existing.in_library = True
            existing.discarded = False
            existing.updated_at = datetime.now(UTC)
            session.add(existing)
            session.commit()
        return ImportPaperOut(id=existing.id, created=False)

    now = datetime.now(UTC)

    if source == "semantic_scholar":
        return _import_from_s2(session, work, now)

    pid = oa.work_id(work)
    if not pid:
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail="OpenAlex returned a work without an id")

    pdf_url, oa_status = oa.work_pdf_url(work)
    paper = Paper(
        id=pid,
        id_kind="openalex",
        title=(work.get("title") or work.get("display_name") or "").strip() or "(untitled)",
        abstract=None,
        publication_date=oa.work_publication_date(work),
        venue=oa.work_venue(work),
        doi=oa.work_doi(work),
        arxiv_id=oa.work_arxiv_id(work),
        pdf_url=pdf_url,
        oa_status=oa_status,
        source=SourceKind.openalex.value,
        status=PaperStatus.pending.value,
        authors=oa.work_authors(work),
        raw_meta=work,
        created_at=now,
        updated_at=now,
    )
    inv = work.get("abstract_inverted_index") or {}
    if inv:
        positions: dict[int, str] = {}
        for word, idxs in inv.items():
            for i in idxs:
                positions[i] = word
        if positions:
            paper.abstract = " ".join(positions[i] for i in sorted(positions))
    session.add(paper)
    session.commit()
    session.refresh(paper)
    return ImportPaperOut(id=paper.id, created=True)


def _import_from_s2(session: Session, work: dict[str, Any], now: datetime) -> ImportPaperOut:
    """Insert a paper directly from a Semantic Scholar record (no OpenAlex Work).

    Used as a fallback when OpenAlex has no copy of the paper. The Carrel id is
    ``"s2:" + paperId``; DOI/arXiv are carried normally so download, citation
    enrichment, and library matching all work.
    """
    s2_pid = work.get("s2_paper_id")
    if not s2_pid:
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail="Semantic Scholar record had no paper id")

    pub_date = work.get("publication_date")
    try:
        parsed_date = datetime.fromisoformat(pub_date).date() if pub_date else None
    except ValueError:
        parsed_date = None

    pdf_url = work.get("pdf_url")
    paper = Paper(
        id=f"s2:{s2_pid}",
        id_kind="semanticscholar",
        title=(work.get("title") or "").strip() or "(untitled)",
        abstract=work.get("abstract"),
        publication_date=parsed_date,
        venue=work.get("venue"),
        doi=work.get("doi"),
        arxiv_id=work.get("arxiv_id"),
        s2_paper_id=s2_pid,
        pdf_url=pdf_url,
        oa_status="oa" if pdf_url else "none",
        source=SourceKind.both.value,
        status=PaperStatus.pending.value,
        authors=work.get("authors") or [],
        raw_meta=work.get("raw") or work,
        created_at=now,
        updated_at=now,
    )
    session.add(paper)
    session.commit()
    session.refresh(paper)
    return ImportPaperOut(id=paper.id, created=True)


def _find_library_match(
    session: Session,
    *,
    oa_id: str | None,
    doi: str | None,
    arxiv_id: str | None,
    s2_id: str | None = None,
) -> Paper | None:
    """Pre-import identifier check. Kept for the import path; search uses the
    batched resolver instead."""
    if oa_id:
        for cand in {oa_id, f"https://openalex.org/{oa_id}"}:
            row = session.get(Paper, cand)
            if row is not None:
                return row
    if s2_id:
        # Matches both s2-kind primary keys and OA papers whose s2_paper_id
        # was populated by citation enrichment.
        row = session.get(Paper, f"s2:{s2_id}")
        if row is not None:
            return row
        row = session.exec(
            select(Paper).where(col(Paper.s2_paper_id) == s2_id)
        ).first()
        if row is not None:
            return row
    if doi:
        doi_variants = {doi, f"https://doi.org/{doi}"}
        rows = session.exec(select(Paper).where(col(Paper.doi).in_(doi_variants))).all()
        if rows:
            return rows[0]
    if arxiv_id:
        row = session.exec(
            select(Paper).where(col(Paper.arxiv_id) == arxiv_id)
        ).first()
        if row is not None:
            return row
    return None
