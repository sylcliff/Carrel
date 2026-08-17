"""Paper list and detail endpoints. Read-only for M1."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from carrel.db import get_session_dep
from carrel.models import Paper
from carrel.schemas import PaperDetail, PaperSummary

router = APIRouter(prefix="/papers", tags=["papers"])


def _to_summary(p: Paper) -> PaperSummary:
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
    )


def _to_detail(p: Paper) -> PaperDetail:
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
        abstract=p.abstract,
        doi=p.doi,
        arxiv_id=p.arxiv_id,
        pdf_url=p.pdf_url,
        pdf_path=p.pdf_path,
        md_path=p.md_path,
        summary_zh=p.summary_zh,
        error=p.error,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@router.get("", response_model=list[PaperSummary])
def list_papers(
    session: Session = Depends(get_session_dep),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, description="Filter by paper status"),
) -> list[PaperSummary]:
    stmt = select(Paper).order_by(Paper.created_at.desc()).offset(offset).limit(limit)
    if status:
        stmt = stmt.where(Paper.status == status)
    return [_to_summary(p) for p in session.exec(stmt).all()]


@router.get("/{paper_id}", response_model=PaperDetail)
def get_paper(
    paper_id: str,
    session: Session = Depends(get_session_dep),
) -> PaperDetail:
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    return _to_detail(p)


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
