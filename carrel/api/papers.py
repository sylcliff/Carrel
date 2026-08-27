"""Paper list, detail, and deletion endpoints."""
from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import String, cast, func
from sqlmodel import Session, col, or_, select

from carrel.db import get_session_dep
from carrel.models import (
    ChatMessage,
    Chunk,
    Paper,
    PaperConcept,
    PaperQuestion,
    PaperTag,
    PaperTopic,
    Tag,
    Topic,
)
from carrel.schemas import AuthorRef, PaperDetail, PaperSummary
from carrel.sources.normalize import format_journal_citation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/papers", tags=["papers"])


def _to_summary(
    p: Paper, tags: list[str] | None = None, topics: list[str] | None = None
) -> PaperSummary:
    return PaperSummary(
        id=p.id,
        title=p.title,
        venue=p.venue,
        publication_date=p.publication_date,
        authors=[a.get("name", "") for a in (p.authors or []) if a.get("name")],
        oa_status=p.oa_status,
        status=p.status,
        tldr_zh=p.tldr_zh,
        tldr_en=p.tldr_en,
        keywords=p.keywords or [],
        source=p.source,
        citation_count=p.citation_count,
        in_library=p.in_library,
        discovered_at=p.discovered_at,
        favorite=p.favorite,
        tags=tags or [],
        topics=topics or [],
        doi=p.doi,
        arxiv_id=p.arxiv_id,
        s2_paper_id=p.s2_paper_id,
    )


def _to_detail(
    p: Paper, tags: list[str] | None = None, topics: list[str] | None = None
) -> PaperDetail:
    return PaperDetail(
        id=p.id,
        title=p.title,
        venue=p.venue,
        publication_date=p.publication_date,
        authors=[a.get("name", "") for a in (p.authors or []) if a.get("name")],
        oa_status=p.oa_status,
        status=p.status,
        tldr_zh=p.tldr_zh,
        tldr_en=p.tldr_en,
        keywords=p.keywords or [],
        source=p.source,
        citation_count=p.citation_count,
        in_library=p.in_library,
        discovered_at=p.discovered_at,
        favorite=p.favorite,
        tags=tags or [],
        topics=topics or [],
        abstract=p.abstract,
        doi=p.doi,
        arxiv_id=p.arxiv_id,
        pdf_url=p.pdf_url,
        pdf_path=p.pdf_path,
        md_path=p.md_path,
        summary_zh=p.summary_zh,
        error=p.error,
        journal_citation=format_journal_citation(p.raw_meta),
        influential_citation_count=p.influential_citation_count,
        reference_count=p.reference_count,
        citations_updated_at=p.citations_updated_at,
        notes_markdown=p.notes_markdown,
        pdf_origin=p.pdf_origin,
        journal_doi=p.journal_doi,
        pdf_files=p.pdf_files,
        published_checked_at=p.published_checked_at,
        created_at=p.created_at,
        updated_at=p.updated_at,
        author_list=[
            AuthorRef(
                name=a.get("name", ""),
                openalex_author_id=a.get("openalex_author_id", "") or "",
                affiliation=a.get("affiliation"),
            )
            for a in (p.authors or [])
            if a.get("name")
        ],
    )


def _load_tags_map(session: Session, paper_ids: list[str]) -> dict[str, list[str]]:
    """Return ``{paper_id: [tag_name, ...]}`` for the given papers in one query.

    Avoids an N+1 when rendering a list of papers with their tags.
    """
    if not paper_ids:
        return {}
    rows = session.exec(
        select(PaperTag.paper_id, Tag.name)
        .join(Tag, Tag.id == PaperTag.tag_id)
        .where(PaperTag.paper_id.in_(paper_ids))
        .order_by(Tag.name)
    ).all()
    out: dict[str, list[str]] = {}
    for pid, name in rows:
        out.setdefault(pid, []).append(name)
    return out


def _load_topics_map(session: Session, paper_ids: list[str]) -> dict[str, list[str]]:
    """Return ``{paper_id: [topic_name, ...]}`` for the given papers in one query."""
    if not paper_ids:
        return {}
    rows = session.exec(
        select(PaperTopic.paper_id, Topic.name)
        .join(Topic, Topic.id == PaperTopic.topic_id)
        .where(PaperTopic.paper_id.in_(paper_ids))
        .order_by(Topic.name)
    ).all()
    out: dict[str, list[str]] = {}
    for pid, name in rows:
        out.setdefault(pid, []).append(name)
    return out


@router.get("", response_model=list[PaperSummary])
def list_papers(
    session: Session = Depends(get_session_dep),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, description="Filter by paper status"),
    venue: str | None = Query(None, description="Case-insensitive substring match on venue name"),
    in_library: bool | None = Query(
        True,
        description="True (default): library papers. False: inbox (discovered, not discarded). Null: all.",
    ),
    favorite: bool | None = Query(None, description="Filter by favorite state"),
    tag: list[str] | None = Query(
        None, description="Filter by tag name(s); repeat for ?tag=a&tag=b (match ANY)"
    ),
    topic: list[str] | None = Query(
        None, description="Filter by topic name(s); repeat for ?topic=a&topic=b (match ANY)"
    ),
    q: str | None = Query(
        None, description="Case-insensitive substring match on title or authors"
    ),
    sort: str = Query("added", description="Sort order"),
    # FastAPI injects the live Response object when the parameter is annotated
    # as `Response`; do NOT union with None (FastAPI rejects that as an
    # invalid Pydantic field type). A default of `Response()` keeps Python
    # happy — the previous parameters all have Query() defaults so this one
    # must too. We use it to publish X-Total-Count.
    response: Response = Response(),
) -> list[PaperSummary]:
    allowed_sorts = {
        "added",
        "updated",
        "pub_newest",
        "pub_oldest",
        "citations",
        "title_az",
        "title_za",
        "favorites",
    }
    if sort not in allowed_sorts:
        sort = "added"
    sort_clauses = {
        "added": [Paper.created_at.desc()],
        "updated": [Paper.updated_at.desc()],
        "pub_newest": [Paper.publication_date.desc().nullslast()],
        "pub_oldest": [Paper.publication_date.asc().nullslast()],
        "citations": [Paper.citation_count.desc().nullslast()],
        "title_az": [func.lower(Paper.title).asc()],
        "title_za": [func.lower(Paper.title).desc()],
        "favorites": [Paper.favorite.desc(), Paper.created_at.desc()],
    }
    # Build the WHERE clauses once and reuse for both the data query and the
    # parallel COUNT(*) that backs the X-Total-Count header. Order/order_by
    # must be cleared for the count or PostgreSQL rejects it.
    where_clauses: list = []
    if status:
        where_clauses.append(Paper.status == status)
    if venue:
        where_clauses.append(Paper.venue.ilike(f"%{venue}%"))
    if in_library is not None:
        where_clauses.append(Paper.in_library.is_(in_library))
        if not in_library:
            # Inbox view hides discarded papers.
            where_clauses.append(Paper.discarded.is_(False))
    if favorite is not None:
        where_clauses.append(Paper.favorite.is_(favorite))
    if q:
        like = f"%{q}%"
        where_clauses.append(
            or_(
                col(Paper.title).ilike(like),
                cast(Paper.authors, String).ilike(like),
            )
        )
    if tag:
        names = [t.strip() for t in tag if t and t.strip()]
        if names:
            tag_subq = (
                select(PaperTag.paper_id)
                .join(Tag, Tag.id == PaperTag.tag_id)
                .where(Tag.name.in_(names))
            )
            where_clauses.append(Paper.id.in_(tag_subq))
    if topic:
        names = [t.strip() for t in topic if t and t.strip()]
        if names:
            topic_subq = (
                select(PaperTopic.paper_id)
                .join(Topic, Topic.id == PaperTopic.topic_id)
                .where(Topic.name.in_(names))
            )
            where_clauses.append(Paper.id.in_(topic_subq))

    total = session.exec(
        select(func.count()).select_from(Paper).where(*where_clauses)
    ).one()
    response.headers["X-Total-Count"] = str(int(total))

    stmt = (
        select(Paper)
        .where(*where_clauses)
        .order_by(*sort_clauses[sort])
        .offset(offset)
        .limit(limit)
    )
    rows = session.exec(stmt).all()
    tags_map = _load_tags_map(session, [p.id for p in rows])
    topics_map = _load_topics_map(session, [p.id for p in rows])
    return [
        _to_summary(p, tags_map.get(p.id, []), topics_map.get(p.id, []))
        for p in rows
    ]


@router.get("/{paper_id}", response_model=PaperDetail)
def get_paper(
    paper_id: str,
    session: Session = Depends(get_session_dep),
) -> PaperDetail:
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    tags_map = _load_tags_map(session, [p.id])
    topics_map = _load_topics_map(session, [p.id])
    return _to_detail(p, tags_map.get(p.id, []), topics_map.get(p.id, []))


@router.post("/{paper_id}/import")
def import_paper_to_library(
    paper_id: str,
    session: Session = Depends(get_session_dep),
) -> dict[str, object]:
    """Move a discovered (inbox) paper into the library.

    Metadata-only: the paper stays ``pending`` and download/parse is a separate
    step. Importing also un-discards a previously discarded paper.
    """
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    p.in_library = True
    p.discarded = False
    p.updated_at = datetime.now(UTC)
    session.add(p)
    session.commit()
    return {"id": p.id, "imported": True, "in_library": True}


@router.post("/{paper_id}/discard")
def discard_paper(
    paper_id: str,
    session: Session = Depends(get_session_dep),
) -> dict[str, object]:
    """Remove a discovered paper from the inbox (soft delete).

    Only valid for inbox (``in_library=False``) papers. Library papers should be
    deleted outright via DELETE. A later sync leaves the discarded flag intact;
    only an explicit import revives it.
    """
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    if p.in_library:
        raise HTTPException(
            status_code=409,
            detail="paper is in the library; use DELETE to remove it instead",
        )
    p.discarded = True
    p.updated_at = datetime.now(UTC)
    session.add(p)
    session.commit()
    return {"id": p.id, "discarded": True}


@router.delete("/{paper_id}")
def delete_paper(
    paper_id: str,
    session: Session = Depends(get_session_dep),
) -> dict[str, object]:
    """Delete a paper: its chunks, DB row, and files on disk.

    The chunks table has no ON DELETE CASCADE, so related rows are removed
    explicitly. Disk cleanup is best-effort — a missing directory must not
    block the DB deletion. Only the paper's own directory under the storage
    papers subdir is removed.
    """
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")

    # The chunks FK has no ON DELETE CASCADE; remove related rows explicitly.
    chunks = session.exec(select(Chunk).where(Chunk.paper_id == paper_id)).all()
    for c in chunks:
        session.delete(c)

    # Same for tag and topic associations.
    links = session.exec(
        select(PaperTag).where(PaperTag.paper_id == paper_id)
    ).all()
    for link in links:
        session.delete(link)
    tp_links = session.exec(
        select(PaperTopic).where(PaperTopic.paper_id == paper_id)
    ).all()
    for link in tp_links:
        session.delete(link)

    # LLM-extracted concept/question rows and any RAG chat transcript also FK
    # to papers.id without ON DELETE CASCADE. Clean them up so the Paper delete
    # doesn't hit a foreign-key violation (which surfaces as a 500).
    concepts = session.exec(
        select(PaperConcept).where(PaperConcept.paper_id == paper_id)
    ).all()
    for row in concepts:
        session.delete(row)
    questions = session.exec(
        select(PaperQuestion).where(PaperQuestion.paper_id == paper_id)
    ).all()
    for row in questions:
        session.delete(row)
    chat_rows = session.exec(
        select(ChatMessage).where(ChatMessage.paper_id == paper_id)
    ).all()
    for row in chat_rows:
        session.delete(row)

    # Resolve the on-disk paper directory before deleting the row.
    from carrel.main import app_config

    storage_root = Path(app_config.storage.root)
    rel_dir: Path | None = None
    for rel in (p.pdf_path, p.md_path):
        if rel:
            rel_dir = (storage_root / rel).parent
            break

    session.delete(p)
    session.commit()

    removed_files = False
    if rel_dir is not None:
        # Safety: only delete a directory that lives under <storage>/papers/.
        papers_root = (storage_root / app_config.storage.papers_subdir).resolve()
        target = rel_dir.resolve()
        if target == papers_root or papers_root not in target.parents:
            logger.warning("refusing to delete %s: outside papers root", target)
        elif target.exists():
            shutil.rmtree(target, ignore_errors=True)
            removed_files = not target.exists()

    return {"id": paper_id, "deleted": True, "removed_files": removed_files}


@router.get("/{paper_id}/markdown")
def get_paper_markdown(
    paper_id: str,
    session: Session = Depends(get_session_dep),
) -> dict[str, str | None]:
    """Return the parsed markdown body if available, else null."""
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    if p.md_path is None:
        raise HTTPException(
            status_code=409,
            detail=f"paper not parsed yet (status={p.status})",
        )

    body: str | None = None
    if p.md_path:
        from pathlib import Path

        full = Path(cfg_storage_root()) / p.md_path
        if full.exists():
            body = full.read_text(encoding="utf-8")
    return {"id": p.id, "body": body, "md_path": p.md_path}


def cfg_storage_root() -> str:
    from carrel.main import app_config

    return str(app_config.storage.root)
