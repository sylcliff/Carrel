"""Rebuild the wiki DB index from Markdown files on disk.

The Markdown files under ``data/wiki/`` are the source of truth; the
``wiki_pages`` table is a rebuildable cache (summary, links, checksum,
embedding aside). :func:`reindex_wiki` walks every page, parses its
frontmatter, and upserts a row; :func:`recompute_backlinks` refreshes the
denormalised incoming-link counts. Embeddings are left intact on reindex (they
are keyed by page id); a reindex never destroys a page's embedding.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from carrel.config import CarrelYAML
from carrel.models import WikiKind, WikiPage
from carrel.pipeline.wiki import _frontmatter, _links, _slug

logger = logging.getLogger(__name__)

_KIND_DIRS = {"concepts": "concept", "scholars": "scholar", "questions": "question"}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_page(storage_root: Path, rel_path: str) -> tuple[dict[str, Any], str] | None:
    """Return (frontmatter, full_text) for a page path, or None if unreadable."""
    full = storage_root / rel_path
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        logger.warning("wiki reindex: cannot read %s", rel_path)
        return None
    meta, _body = _frontmatter.parse(text)
    return meta, text


def reindex_wiki(session: Session, cfg: CarrelYAML) -> dict[str, int]:
    """Walk ``data/wiki`` and upsert a WikiPage row per file.

    Returns counters ``{"indexed": n, "orphaned_rows": n}``. Rows whose file no
    longer exists are left in place (they may be mid-compile); callers can prune
    separately if desired.
    """
    root = Path(cfg.storage.root)
    indexed = 0
    seen_paths: set[str] = set()

    for dirname, kind in _KIND_DIRS.items():
        kind_dir = root / "wiki" / dirname
        if not kind_dir.is_dir():
            continue
        for md_file in sorted(kind_dir.glob("*.md")):
            if md_file.name == "_index.md":
                continue
            rel = md_file.relative_to(root).as_posix()
            seen_paths.add(rel)
            parsed = _read_page(root, rel)
            if parsed is None:
                continue
            meta, text = parsed
            slug = md_file.stem
            _upsert_row(
                session,
                kind=kind,
                slug=slug,
                rel_path=rel,
                meta=meta,
                text=text,
            )
            indexed += 1

    session.commit()
    return {"indexed": indexed, "files_seen": len(seen_paths)}


def _upsert_row(
    session: Session,
    *,
    kind: str,
    slug: str,
    rel_path: str,
    meta: dict[str, Any],
    text: str,
) -> WikiPage:
    """Upsert a WikiPage row from frontmatter + parsed links."""
    page = session.exec(
        select(WikiPage).where(WikiPage.kind == kind, WikiPage.slug == slug)
    ).first()
    now = datetime.now(UTC)
    links = [href for _label, href in _links.extract_wikilinks(text)]
    source_ids = meta.get("source_paper_ids") or []
    if not isinstance(source_ids, list):
        source_ids = []
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    if page is None:
        page = WikiPage(
            kind=kind,
            slug=slug,
            path=rel_path,
            created_at=now,
        )
        session.add(page)

    page.title = meta.get("title") or slug
    page.path = rel_path
    page.checksum = _sha256(text)
    page.summary = meta.get("summary")
    page.tags = [str(t) for t in tags]
    page.links_out = links
    page.source_paper_ids = [str(p) for p in source_ids]
    page.confidence = float(meta.get("confidence") or 0.0)
    page.evidence_count = int(meta.get("evidence_count") or 0)
    if kind == WikiKind.scholar.value:
        aid = meta.get("openalex_id")
        page.scholar_aid = aid if isinstance(aid, str) and aid.startswith("A") else None
    if kind == WikiKind.question.value:
        status = meta.get("status")
        page.question_status = status if isinstance(status, str) else None
    compiled_at = meta.get("compiled_at")
    if compiled_at:
        page.compiled_at = compiled_at if hasattr(compiled_at, "tzinfo") else now
    else:
        page.compiled_at = now
    page.updated_at = now
    return page


def upsert_page_from_disk(
    session: Session, cfg: CarrelYAML, kind: str, slug: str
) -> WikiPage | None:
    """Upsert a single page row after the compiler writes its file."""
    rel = _slug.page_path(kind, slug)
    parsed = _read_page(Path(cfg.storage.root), rel)
    if parsed is None:
        return None
    meta, text = parsed
    page = _upsert_row(
        session, kind=kind, slug=slug, rel_path=rel, meta=meta, text=text
    )
    session.commit()
    return page


def recompute_backlinks(session: Session) -> int:
    """Refresh ``links_in_count`` for every page from current ``links_out``.

    Returns the number of pages touched.
    """
    pages = session.exec(select(WikiPage)).all()
    by_kind_slug = {(p.kind, p.slug): p for p in pages}
    counts: dict[int, int] = {}
    for p in pages:
        for href in p.links_out or []:
            target = _links.resolve_link(p.path, href)
            if target is None:
                continue
            tpage = by_kind_slug.get(target)
            if tpage is not None:
                counts[tpage.id] = counts.get(tpage.id, 0) + 1
    touched = 0
    for p in pages:
        new_count = counts.get(p.id, 0)
        if p.links_in_count != new_count:
            p.links_in_count = new_count
            touched += 1
    session.commit()
    return touched
