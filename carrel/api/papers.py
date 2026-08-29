"""Paper list, detail, and deletion endpoints."""
from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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
from carrel.chunking import split_by_heading
from carrel.schemas import (
    AuthorRef,
    PaperDetail,
    PaperSections,
    PaperSummary,
    SectionOut,
)
from carrel.sources.normalize import format_journal_citation
from carrel.api._app_cache import cached
from carrel.api._invalidation import invalidate_paper_mutated, invalidate_bulk_import_done
from carrel.api._http_cache import (
    apply_etag_headers,
    etag_for_list,
    etag_for_updated_at,
    if_none_match_matches,
)

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
        updated_at=p.updated_at,
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


@cached(
    "papers_list",
    key_params=(
        "status", "venue", "in_library", "favorite", "tag", "topic",
        "q", "sort", "limit", "offset",
    ),
    tags=("papers_list", "tags", "topics", "scholars_list"),
    offset_invariant=True,
)
def _list_papers_body(
    session: Session,
    limit: int,
    offset: int,
    status: str | None,
    venue: str | None,
    in_library: bool | None,
    favorite: bool | None,
    tag: list[str] | None,
    topic: list[str] | None,
    q: str | None,
    sort: str,
) -> tuple[list[PaperSummary], int]:
    """Cached (rows, total) tuple. Filter fingerprint is in the cache key.

    The route handler sets X-Total-Count from ``total`` and computes the
    ETag from the row set. L2 is the long-lived fan-out target; L1
    is the short-lived per-request check.
    """
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
    items = [
        _to_summary(p, tags_map.get(p.id, []), topics_map.get(p.id, []))
        for p in rows
    ]
    return items, int(total)


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
    items, total = _list_papers_body(
        session, limit, offset, status, venue, in_library,
        favorite, tag, topic, q, sort,
    )
    response.headers["X-Total-Count"] = str(total)

    # Layer 1 ETag for the list. Embed the row set, the total count, and
    # the filter fingerprint so a refresh after a write (which would
    # change the row set or the total) produces a different ETag.
    if items:
        max_updated = max(
            (p.updated_at for p in items if p.updated_at is not None),
            default=None,
        )
        etag = etag_for_list(
            max_updated_at=max_updated,
            row_ids=[p.id for p in items],
            count=total,
        )
    else:
        # Empty list: still a valid ETag (count=0 is stable until a row
        # is added or removed).
        etag = etag_for_list(
            max_updated_at=None,
            row_ids=[],
            count=total,
        )
    if etag is not None:
        # Library list is short-lived — the same filter combination with
        # new rows should revalidate fast.
        apply_etag_headers(response, etag, max_age=15, stale_while_revalidate=30)
    return items


@cached("paper", key_params=("paper_id",), tags=("paper", "papers_list"))
def _get_paper_body(paper_id: str, session: Session) -> PaperDetail:
    """Cached paper body. Raises 404 when the id is unknown.

    The 404 is *not* cached because the decorator only stores truthy return
    values. A misspelled id will continue to hit the DB on every request,
    which is the right behavior (no negative caching, no thundering herd).
    """
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    tags_map = _load_tags_map(session, [p.id])
    topics_map = _load_topics_map(session, [p.id])
    return _to_detail(p, tags_map.get(p.id, []), topics_map.get(p.id, []))


@router.get("/{paper_id}", response_model=PaperDetail)
def get_paper(
    paper_id: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session_dep),
) -> PaperDetail:
    # Layer 1 + 2: the body is memoized in-process; we build the ETag from
    # the returned body so cache hits still serve correct 304s. The
    # ``updated_at`` on the cached body reflects the row at the time of the
    # last cache miss; an external write that calls
    # ``invalidate_paper_mutated`` will drop the entry and the next
    # request will see the new timestamp.
    body = _get_paper_body(paper_id, session)
    etag = etag_for_updated_at(body.updated_at, extra=(body.id,))
    if etag is not None and if_none_match_matches(request, etag):
        return Response(  # type: ignore[return-value]
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "private, max-age=60, stale-while-revalidate=120",
            },
        )
    if etag is not None:
        apply_etag_headers(response, etag, max_age=60, stale_while_revalidate=120)
    return body


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
    # L2: per-paper detail + library/inbox list fan-out.
    invalidate_paper_mutated(paper_id, mutate={"inbox"})
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
    # L2: per-paper detail + inbox list fan-out.
    invalidate_paper_mutated(paper_id, mutate={"discarded"})
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

    # L2: full per-paper eviction + list fan-out.
    invalidate_paper_mutated(paper_id, mutate={"deleted"})
    return {"id": paper_id, "deleted": True, "removed_files": removed_files}


@cached("paper_markdown", key_params=("paper_id",), tags=("paper", "paper:markdown", "papers_list"))
def _get_paper_markdown_body(
    paper_id: str, session: Session
) -> tuple[dict[str, str | None], datetime | None]:
    """Cached (markdown body, paper row updated_at) tuple.

    404 / 409 are not cached (decorator only stores truthy returns). The
    body is immutable once parsed and large, so the L1 layer gives it a
    10-minute ``max-age`` and a 24-hour stale-while-revalidate. The
    pipeline / write paths invalidate this entry on re-parse via
    ``invalidate_paper_mutated``.
    """
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
    return {"id": p.id, "body": body, "md_path": p.md_path}, p.updated_at


@router.get("/{paper_id}/markdown")
def get_paper_markdown(
    paper_id: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session_dep),
) -> dict[str, str | None]:
    """Return the parsed markdown body if available, else null.

    Layer 1: markdown is large and immutable once parsed, so we set a
    long max-age (10 min) and a generous stale-while-revalidate
    (24 hours). The ETag is built from the row's ``updated_at`` and
    ``md_path``; an external write that calls
    ``invalidate_paper_mutated`` drops the cached body and the next
    request sees a new ETag.
    """
    body, updated_at = _get_paper_markdown_body(paper_id, session)
    etag = etag_for_updated_at(
        updated_at, extra=(body["id"], body.get("md_path") or "")
    )
    if etag is not None and if_none_match_matches(request, etag):
        return Response(  # type: ignore[return-value]
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, max-age=600, stale-while-revalidate=86400"},
        )
    if etag is not None:
        apply_etag_headers(
            response, etag, max_age=600, stale_while_revalidate=86400,
        )
    return body


@cached("paper_sections", key_params=("paper_id",), tags=("paper", "paper:sections", "papers_list"))
def _get_paper_sections_body(
    paper_id: str, session: Session
) -> tuple[dict[str, object], datetime | None]:
    """Cached ((id, sections, md_path), updated_at) tuple.

    Splits the parsed Markdown by ATX heading into ``(heading_path, body)``
    pairs in document order (see :func:`carrel.chunking.split_by_heading`).
    404 / 409 are not cached. Same cache contract as the markdown
    endpoint: the body is large and immutable once parsed, so the L1
    layer gives it a 10-minute ``max-age`` and 24-hour SWR. Re-parse
    invalidates the per-id exact drop through
    ``invalidate_paper_mutated(paper_id, mutate={"parse"})``.
    """
    p = session.get(Paper, paper_id)
    if p is None:
        raise HTTPException(status_code=404, detail="paper not found")
    if p.md_path is None:
        raise HTTPException(
            status_code=409,
            detail=f"paper not parsed yet (status={p.status})",
        )

    sections: list[SectionOut] = []
    full = Path(cfg_storage_root()) / p.md_path
    if full.exists():
        md = full.read_text(encoding="utf-8")
        for i, (heading, body) in enumerate(split_by_heading(md)):
            leaf = heading.split(" / ")[-1] if heading else ""
            sections.append(
                SectionOut(
                    index=i,
                    heading=leaf,
                    heading_path=heading,
                    body=body,
                    char_count=len(body),
                )
            )
    return (
        PaperSections(
            id=p.id,
            sections=sections,
            md_path=p.md_path,
        ).model_dump(mode="json"),
        p.updated_at,
    )


@router.get("/{paper_id}/sections")
def get_paper_sections(
    paper_id: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session_dep),
) -> dict[str, object]:
    """Return the parsed paper split by heading, in document order.

    Mirrors the cache contract of ``GET /{paper_id}/markdown``: L2
    ``@cached`` (route id ``paper_sections``) and L1 ETag derived from
    the paper row's ``updated_at`` plus ``md_path``. Body is large and
    immutable once parsed, so we ship the same
    ``max-age=600, stale-while-revalidate=86400`` headers.
    """
    body, updated_at = _get_paper_sections_body(paper_id, session)
    etag = etag_for_updated_at(
        updated_at, extra=(body["id"], body.get("md_path") or "")
    )
    if etag is not None and if_none_match_matches(request, etag):
        return Response(  # type: ignore[return-value]
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, max-age=600, stale-while-revalidate=86400"},
        )
    if etag is not None:
        apply_etag_headers(
            response, etag, max_age=600, stale_while_revalidate=86400,
        )
    return body


def cfg_storage_root() -> str:
    from carrel.main import app_config

    return str(app_config.storage.root)
