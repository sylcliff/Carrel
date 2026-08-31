"""User annotations: favorites, notes, and tags.

Single-user app — there is no user_id. Favorites and notes live as columns on
``papers``; tags are a many-to-many via the ``paper_tags`` association table.

The router has no prefix so it can mix paper-scoped routes (``/papers/{id}/...``,
alongside the citations router) with tag-list routes (``/tags``).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, func
from sqlmodel import Session, select

from carrel.db import get_session_dep
from carrel.api._app_cache import cached, get_cache
from carrel.api._invalidation import invalidate_paper_mutated
from carrel.api._http_cache import (
    apply_etag_headers,
    etag_for_updated_at,
    if_none_match_matches,
    maybe_return_304,
)
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
    # L2: fan out to the per-paper detail entry, the papers list, and the
    # scholars aggregation (which surfaces favorite counts per author).
    invalidate_paper_mutated(paper_id, mutate={"favorite"})
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
    p.notes_markdown = body.notes_markdown.strip() or None
    p.updated_at = datetime.now(UTC)
    session.add(p)
    session.commit()
    # L2: per-paper detail + list views.
    invalidate_paper_mutated(paper_id, mutate={"notes"})
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


@cached("paper_tags", key_params=("paper_id",), tags=("paper", "paper:tags", "tags"))
def _list_paper_tags_body(paper_id: str, session: Session) -> list[TagOut]:
    """Cached list of tags for one paper. Raises 404 when the paper is unknown."""
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


@router.get("/papers/{paper_id}/tags", response_model=list[TagOut])
def list_paper_tags(
    paper_id: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session_dep),
) -> list[TagOut]:
    """Tags attached to one paper.

    Layer 1: ETag is derived from the paper's ``updated_at`` because
    every tag attach / detach mutates the paper row (the FK lives on
    ``PaperTag`` but the API bumps ``papers.updated_at`` on
    annotation changes too — see ``set_favorite`` / ``set_notes``).
    Layer 2: the body is memoized in-process; the route handler
    re-reads ``updated_at`` to build a stable ETag (a single
    indexed lookup, not the join).
    """
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    tags = _list_paper_tags_body(paper_id, session)
    etag = etag_for_updated_at(p.updated_at, extra=(p.id,))
    if (r := maybe_return_304(request, etag, max_age=60, stale_while_revalidate=120)):
        return r
    if etag is not None:
        apply_etag_headers(response, etag, max_age=60, stale_while_revalidate=120)
    return tags


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
    # L2: per-paper tag list + global tag aggregation.
    invalidate_paper_mutated(paper_id, mutate={"tags"})
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
    # L2: per-paper tag list + global tag aggregation.
    invalidate_paper_mutated(paper_id, mutate={"tags"})
    return {"id": tag_id, "paper_id": paper_id, "detached": True}


@router.get("/tags", response_model=list[TagWithCount])
def list_tags(
    response: Response,
    session: Session = Depends(get_session_dep),
) -> list[TagWithCount]:
    """All tags with the number of papers carrying each (zero counts included).

    Layer 1: counts are a global aggregation, so a precise ETag would
    require a per-row fingerprint. Use a short max-age instead and
    rely on L2 invalidation in Phase 3 for precision after writes.
    """
    response.headers["Cache-Control"] = "private, max-age=10, stale-while-revalidate=30"
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
    # Collect the affected paper ids BEFORE we delete the links so the
    # per-paper detail entries can be invalidated. One query; the
    # 1-tuple-or-scalar coercion handles both SQLite and Postgres.
    affected_paper_ids = [
        pid
        for pid in (
            row[0] if isinstance(row, tuple) else row
            for row in session.exec(
                select(PaperTag.paper_id).where(PaperTag.tag_id == tag_id)
            ).all()
        )
    ]
    # Single DELETE statement instead of N row-by-row session.delete()s.
    # SQLAlchemy's `delete(PaperTag).where(...)` issues one round-trip
    # for all links tagged with this id.
    result = session.exec(
        delete(PaperTag).where(PaperTag.tag_id == tag_id)
    )
    detached = result.rowcount or 0
    session.delete(tag)
    session.commit()
    # L2: drop the global tag aggregation and every per-paper sub-resource
    # for the affected papers. The per-id drop is a single tag match per
    # paper (``paper_id:{pid}`` is auto-attached by the ``@cached``
    # decorator), which catches paper, markdown, sections, references,
    # tags, card — all in one fan-out.
    cache = get_cache()
    cache.invalidate_tags("tags", "papers_list")
    if affected_paper_ids:
        cache.invalidate_tags(*(f"paper_id:{pid}" for pid in affected_paper_ids))
    return {"id": tag_id, "deleted": True, "detached": detached}
