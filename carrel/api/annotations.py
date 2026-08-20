"""User annotations: favorites, notes, and tags.

Single-user app — there is no user_id. Favorites and notes live as columns on
``papers``; tags are a many-to-many via the ``paper_tags`` association table.

The router has no prefix so it can mix paper-scoped routes (``/papers/{id}/...``,
alongside the citations router) with tag-list routes (``/tags``).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from carrel.db import get_session_dep
from carrel.models import Paper, PaperTag, Tag
from carrel.schemas import (
    FavoriteIn,
    FavoriteOut,
    NotesIn,
    NotesOut,
    TagIn,
    TagOut,
    TagWithCount,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["annotations"])


# ------------------ Favorites ------------------


@router.post("/papers/{paper_id}/favorite", response_model=FavoriteOut)
def set_favorite(
    paper_id: str,
    body: FavoriteIn,
    session: Session = Depends(get_session_dep),
) -> FavoriteOut:
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    p.favorite = body.favorite
    p.updated_at = datetime.now(UTC)
    session.add(p)
    session.commit()
    return FavoriteOut(id=p.id, favorite=p.favorite)


# ------------------ Notes ------------------


@router.put("/papers/{paper_id}/notes", response_model=NotesOut)
def set_notes(
    paper_id: str,
    body: NotesIn,
    session: Session = Depends(get_session_dep),
) -> NotesOut:
    """Replace the paper's note with ``notes_markdown`` (whole-body PUT).

    An empty/whitespace string clears the note (stored as None). Bumps
    ``updated_at`` so recently-annotated papers surface in recent-first views.
    """
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    content = body.notes_markdown.strip()
    p.notes_markdown = content or None
    p.updated_at = datetime.now(UTC)
    session.add(p)
    session.commit()
    return NotesOut(
        id=p.id, notes_markdown=p.notes_markdown, updated_at=p.updated_at
    )


# ------------------ Tags ------------------


def _normalize_tag_name(name: str) -> str | None:
    """Collapse internal whitespace; return None for an empty tag."""
    normalized = " ".join(name.strip().split())
    return normalized or None


def _get_or_create_tag(session: Session, name: str) -> Tag:
    """Find a tag by name case-insensitively, or create a new one.

    Preserves the casing first used for a tag. The unique constraint is on the
    exact name; the ilike lookup keeps "NLP" and "nlp" from diverging in
    practice (single-user, so a race is negligible).
    """
    existing = session.exec(select(Tag).where(Tag.name.ilike(name))).first()
    if existing is not None:
        return existing
    tag = Tag(name=name)
    session.add(tag)
    session.commit()
    session.refresh(tag)
    return tag


@router.get("/papers/{paper_id}/tags", response_model=list[TagOut])
def list_paper_tags(
    paper_id: str,
    session: Session = Depends(get_session_dep),
) -> list[TagOut]:
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    rows = session.exec(
        select(Tag)
        .join(PaperTag, PaperTag.tag_id == Tag.id)
        .where(PaperTag.paper_id == paper_id)
        .order_by(Tag.name)
    ).all()
    return [TagOut(id=t.id, name=t.name) for t in rows]  # type: ignore[arg-type]


@router.post("/papers/{paper_id}/tags", response_model=TagOut)
def add_paper_tag(
    paper_id: str,
    body: TagIn,
    session: Session = Depends(get_session_dep),
) -> TagOut:
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    name = _normalize_tag_name(body.name)
    if name is None:
        raise HTTPException(status_code=422, detail="tag name must not be empty")
    tag = _get_or_create_tag(session, name)
    existing = session.get(PaperTag, (paper_id, tag.id))  # type: ignore[arg-type]
    if existing is None:
        session.add(PaperTag(paper_id=paper_id, tag_id=tag.id))  # type: ignore[arg-type]
        session.commit()
    return TagOut(id=tag.id, name=tag.name)  # type: ignore[arg-type]


@router.delete("/papers/{paper_id}/tags/{tag_id}")
def remove_paper_tag(
    paper_id: str,
    tag_id: int,
    session: Session = Depends(get_session_dep),
) -> dict[str, object]:
    link = session.get(PaperTag, (paper_id, tag_id))
    if link is None:
        raise HTTPException(status_code=404, detail="tag not attached to paper")
    session.delete(link)
    session.commit()
    return {"id": tag_id, "paper_id": paper_id, "detached": True}


@router.get("/tags", response_model=list[TagWithCount])
def list_tags(
    session: Session = Depends(get_session_dep),
) -> list[TagWithCount]:
    """All tags with the number of papers carrying each (zero counts included)."""
    rows = session.exec(
        select(Tag.id, Tag.name, func.count(PaperTag.paper_id))
        .outerjoin(PaperTag, PaperTag.tag_id == Tag.id)
        .group_by(Tag.id, Tag.name)
        .order_by(Tag.name)
    ).all()
    return [
        TagWithCount(id=t_id, name=name, paper_count=count or 0)
        for t_id, name, count in rows
    ]


@router.delete("/tags/{tag_id}")
def delete_tag(
    tag_id: int,
    session: Session = Depends(get_session_dep),
) -> dict[str, object]:
    """Delete a tag and detach it from all papers."""
    tag = session.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="tag not found")
    links = session.exec(select(PaperTag).where(PaperTag.tag_id == tag_id)).all()
    detached = len(links)
    for link in links:
        session.delete(link)
    session.delete(tag)
    session.commit()
    return {"id": tag_id, "deleted": True, "detached": detached}
