"""Paper list, detail, and deletion endpoints."""
from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, func
from sqlmodel import Session, col, or_, select

from carrel.db import get_session_dep
from carrel.models import Chunk, Paper, PaperTag, Tag
from carrel.schemas import PaperDetail, PaperSummary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/papers", tags=["papers"])


def _to_summary(p: Paper, tags: list[str] | None = None) -> PaperSummary:
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
    )


def _to_detail(p: Paper, tags: list[str] | None = None) -> PaperDetail:
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
        abstract=p.abstract,
        doi=p.doi,
        arxiv_id=p.arxiv_id,
        pdf_url=p.pdf_url,
        pdf_path=p.pdf_path,
        md_path=p.md_path,
        summary_zh=p.summary_zh,
        error=p.error,
        influential_citation_count=p.influential_citation_count,
        reference_count=p.reference_count,
        citations_updated_at=p.citations_updated_at,
        notes_markdown=p.notes_markdown,
        created_at=p.created_at,
        updated_at=p.updated_at,
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
    q: str | None = Query(
        None, description="Case-insensitive substring match on title or authors"
    ),
    sort: str = Query("added", description="Sort order"),
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
    stmt = (
        select(Paper)
        .order_by(*sort_clauses[sort])
        .offset(offset)
        .limit(limit)
    )
    if status:
        stmt = stmt.where(Paper.status == status)
    if venue:
        stmt = stmt.where(Paper.venue.ilike(f"%{venue}%"))
    if in_library is not None:
        stmt = stmt.where(Paper.in_library.is_(in_library))
        if not in_library:
            # Inbox view hides discarded papers.
            stmt = stmt.where(Paper.discarded.is_(False))
    if favorite is not None:
        stmt = stmt.where(Paper.favorite.is_(favorite))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
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
            stmt = stmt.where(Paper.id.in_(tag_subq))
    rows = session.exec(stmt).all()
    tags_map = _load_tags_map(session, [p.id for p in rows])
    return [_to_summary(p, tags_map.get(p.id, [])) for p in rows]


@router.get("/{paper_id}", response_model=PaperDetail)
def get_paper(
    paper_id: str,
    session: Session = Depends(get_session_dep),
) -> PaperDetail:
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    tags_map = _load_tags_map(session, [p.id])
    return _to_detail(p, tags_map.get(p.id, []))


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

    # Same for tag associations.
    links = session.exec(
        select(PaperTag).where(PaperTag.paper_id == paper_id)
    ).all()
    for link in links:
        session.delete(link)

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
