"""Search endpoints (M5).

Local results come from a SQL LIKE on title/abstract/authors against the
``papers`` table. External results are an OpenAlex ``Works().search()`` call,
with an in-library flag so the UI can fold duplicates into the local section.
Full-text results come from a cosine-similarity search over embedded chunks
(pgvector on Postgres, in-memory scan on SQLite).
"""
from __future__ import annotations

import json
import logging
import math
import threading
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, cast
from sqlmodel import Session, col, or_, select

from carrel import embeddings as emb
from carrel import spelling
from carrel.db import get_session_dep
from carrel.models import Chunk, Paper, PaperStatus, SourceKind
from carrel.schemas import (
    ExternalSearchResult,
    ImportPaperIn,
    ImportPaperOut,
    LocalSearchResult,
    SearchResponse,
    SemanticSearchHit,
    SemanticSearchResponse,
    SemanticSearchResult,
)
from carrel.sources import openalex_client as oa

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

# Seeded once per process. Lazy — first search pays the cost (~100ms for a
# few thousand papers) so startup stays fast.
_seeded = False
_seed_lock = threading.Lock()


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


def _find_library_match(
    session: Session, *, oa_id: str | None, doi: str | None, arxiv_id: str | None,
) -> Paper | None:
    if oa_id:
        for cand in {oa_id, f"https://openalex.org/{oa_id}"}:
            row = session.get(Paper, cand)
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


def _local_search(session: Session, q: str, limit: int) -> list[LocalSearchResult]:
    """SQL LIKE search on title/abstract/authors.

    ponytail: SQLite FTS5 would be the proper fix (relevance, stemming) but
    for a single-user library of a few thousand rows the LIKE scan is fast
    enough and keeps the schema unchanged.
    """
    pattern = f"%{q}%"
    stmt = (
        select(Paper)
        .where(
            or_(
                col(Paper.title).ilike(pattern),
                col(Paper.abstract).ilike(pattern),
                cast(Paper.authors, String).ilike(pattern),
                col(Paper.doi).ilike(pattern),
                col(Paper.arxiv_id).ilike(pattern),
            )
        )
        .order_by(Paper.updated_at.desc())
        .limit(limit)
    )
    rows = session.exec(stmt).all()
    out: list[LocalSearchResult] = []
    for p in rows:
        abstract = p.abstract
        snippet = _abstract_excerpt(abstract, q)
        out.append(LocalSearchResult(
            id=p.id,
            title=p.title,
            abstract=abstract,
            authors=_author_names(p),
            venue=p.venue,
            publication_date=p.publication_date.isoformat() if p.publication_date else None,
            doi=p.doi,
            arxiv_id=p.arxiv_id,
            citation_count=p.citation_count,
            status=p.status,
            snippet=snippet,
        ))
    return out


def _external_search(
    session: Session, q: str, limit: int,
) -> list[ExternalSearchResult]:
    try:
        results = oa.Works().search(q).get(per_page=min(limit, 50))
    except Exception as e:  # noqa: BLE001
        logger.warning("openalex search failed for %r: %s", q, e)
        return []

    out: list[ExternalSearchResult] = []
    for w in results:
        oa_id = oa.work_id(w) or ""
        doi = oa.work_doi(w)
        arxiv = oa.work_arxiv_id(w)
        lib = _find_library_match(session, oa_id=oa_id, doi=doi, arxiv_id=arxiv)

        abstract_inverted = w.get("abstract_inverted_index") or {}
        abstract: str | None = None
        if abstract_inverted:
            positions: dict[int, str] = {}
            for word, idxs in abstract_inverted.items():
                for i in idxs:
                    positions[i] = word
            if positions:
                abstract = " ".join(positions[i] for i in sorted(positions))
        snippet = _abstract_excerpt(abstract, q)

        pdf_url, oa_status = oa.work_pdf_url(w)
        out.append(ExternalSearchResult(
            openalex_id=oa_id,
            title=(w.get("title") or w.get("display_name") or "").strip(),
            abstract=abstract,
            authors=[
                (a.get("author") or {}).get("display_name") or ""
                for a in (w.get("authorships") or [])
            ],
            venue=oa.work_venue(w),
            publication_date=str(w.get("publication_year")) if w.get("publication_year") else None,
            doi=doi,
            arxiv_id=arxiv,
            citation_count=w.get("cited_by_count"),
            cited_by_url=f"https://openalex.org/works/{oa_id}" if oa_id else None,
            in_library=lib is not None,
            library_id=lib.id if lib is not None else None,
            pdf_url=pdf_url if oa_status == "oa" else None,
            snippet=snippet,
        ))
    return out


@router.get("/search/local", response_model=list[LocalSearchResult])
def search_local(
    q: str = Query("", min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    correct: bool = Query(True),
    session: Session = Depends(get_session_dep),
) -> list[LocalSearchResult]:
    q = q.strip()
    if not q:
        return []
    if correct:
        q, _ = _apply_correction(q, session)
    return _local_search(session, q, limit)


@router.get("/search/external", response_model=list[ExternalSearchResult])
def search_external(
    q: str = Query("", min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=50),
    correct: bool = Query(True),
    session: Session = Depends(get_session_dep),
) -> list[ExternalSearchResult]:
    q = q.strip()
    if not q:
        return []
    if correct:
        q, _ = _apply_correction(q, session)
    return _external_search(session, q, limit)


@router.get("/search", response_model=SearchResponse)
def search_combined(
    q: str = Query("", min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=50),
    correct: bool = Query(True),
    session: Session = Depends(get_session_dep),
) -> SearchResponse:
    """One-shot: local + external. Frontend uses this for unified view."""
    q = q.strip()
    if not q:
        return SearchResponse(query="", local=[], external=[])
    if correct:
        corrected, original = _apply_correction(q, session)
    else:
        corrected, original = q, None
    return SearchResponse(
        query=corrected,
        corrected_from=original,
        local=_local_search(session, corrected, limit),
        external=_external_search(session, corrected, limit),
    )


# ---------------------------------------------------------------------------
# Full-text semantic search (M5)
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
    """Run a full-text query and group chunk hits by paper.

    ponytail: embeds the query and a top-K scan over the chunks table; groups
    hits by paper (max 3 chunks per paper) so the UI shows one card per paper.
    No re-ranker — we trust cosine + per-paper best-score ordering.
    """
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

    # Pull K=3x limit chunks so per-paper grouping still has headroom.
    raw_limit = min(limit * 3, 100)
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        hits = _semantic_search_postgres(session, q_vec, raw_limit)
    else:
        hits = _semantic_search_sqlite(session, q_vec, raw_limit)

    # Group by paper; keep top-3 chunks per paper, top-N papers overall.
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
    """Full-text semantic search over embedded chunks (M5)."""
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


@router.post("/import", response_model=ImportPaperOut)
def import_external_paper(
    body: ImportPaperIn,
    session: Session = Depends(get_session_dep),
) -> ImportPaperOut:
    """Fetch an external work from OpenAlex and upsert it into the library.

    Accepts any of: `openalex_id` (W… or full URL), `doi`, or `arxiv_id`.
    Returns the resulting library Paper id and whether it was new.
    """
    oa_id = body.openalex_id
    doi = body.doi
    arxiv_id = body.arxiv_id

    # Resolve to an OpenAlex Work. Prefer W-id, fall back to DOI lookup, then
    # arXiv lookup. We need a Work object to extract normalized fields.
    work: dict | None = None
    if oa_id:
        oa_id = oa_id.rsplit("/", 1)[-1]  # strip https://openalex.org/
        work = oa.lookup_by_doi(oa_id)  # pyalex Works()[id] handles W-ids too
        # lookup_by_doi calls Works()[doi] which doesn't accept W-ids; retry
        # via a direct fetch if needed.
        if not work or oa_id.startswith("W"):
            try:
                w = oa.Works()[oa_id]
                work = dict(w) if w else None
            except Exception as e:  # noqa: BLE001
                logger.warning("openalex lookup W=%s failed: %s", oa_id, e)
                work = None
    if work is None and doi:
        work = oa.lookup_by_doi(doi)
    if work is None and arxiv_id:
        work = oa.lookup_by_arxiv_id(arxiv_id)

    if not work:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="could not resolve that identifier on OpenAlex")

    # If it's already in the library under any matching identifier, return that.
    existing = _find_library_match(
        session,
        oa_id=oa.work_id(work),
        doi=oa.work_doi(work),
        arxiv_id=oa.work_arxiv_id(work),
    )
    if existing is not None:
        return ImportPaperOut(id=existing.id, created=False)

    from datetime import UTC, datetime
    now = datetime.now(UTC)
    pid = oa.work_id(work)
    if not pid:
        # OpenAlex id missing; refuse rather than silently use a placeholder.
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail="OpenAlex returned a work without an id")

    pdf_url, oa_status = oa.work_pdf_url(work)
    paper = Paper(
        id=pid,
        id_kind="openalex",
        title=(work.get("title") or work.get("display_name") or "").strip() or "(untitled)",
        abstract=None,  # filled below from inverted index
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
    # Reconstruct abstract.
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
