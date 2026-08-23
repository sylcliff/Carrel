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
    # Clear the resolve_target cache: a reindex may have moved pages (the
    # canonical's slug changed, a new redirect shell was written), so prior
    # cached target ids point at stale rows.
    _links.clear_resolve_cache()

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
    """Upsert a WikiPage row from frontmatter + parsed links.

    Recognizes a ``redirects_to`` frontmatter key and turns the row into a
    redirect shell: ``entity_key`` is cleared, content mirrors are zeroed,
    and wikilinks inside the stub body are ignored (the body is a single
    line and counting it would double-count backlinks).  ``entity_key`` from
    frontmatter is mirrored to the column for non-redirect rows so manual
    renames survive a reindex.
    """
    page = session.exec(
        select(WikiPage).where(WikiPage.kind == kind, WikiPage.slug == slug)
    ).first()
    now = datetime.now(UTC)
    source_ids = meta.get("source_paper_ids") or []
    if not isinstance(source_ids, list):
        source_ids = []
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    redirects_to = meta.get("redirects_to")
    is_redirect = isinstance(redirects_to, str) and redirects_to.strip() != ""

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
    if is_redirect:
        # Redirect shell: collapse content fields, remember the target.
        page.redirects_to = redirects_to.strip()
        page.entity_key = None
        page.summary = None
        page.tags = []
        page.links_out = []
        page.source_paper_ids = []
        page.confidence = 0.0
        page.evidence_count = 0
    else:
        # Live page: extract wikilinks from the body (the stub body of a
        # redirect shell is one line, so excluding it is automatic when the
        # file is also a shell).
        links = [href for _label, href in _links.extract_wikilinks(text)]
        page.summary = meta.get("summary")
        page.tags = [str(t) for t in tags]
        page.links_out = links
        page.source_paper_ids = [str(p) for p in source_ids]
        page.confidence = float(meta.get("confidence") or 0.0)
        page.evidence_count = int(meta.get("evidence_count") or 0)
        # Mirror the canonical entity_key if the file declares it. Falls
        # back to a kind-aware derivation (scholar_aid for scholars) so
        # legacy pages without an explicit entity_key still get a key.
        page.entity_key = _entity_key_from_meta(kind, meta)
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


def _entity_key_from_meta(kind: str, meta: dict[str, Any]) -> str | None:
    """Resolve the canonical ``entity_key`` from frontmatter fields.

    Priority: explicit ``entity_key`` → scholar_aid (scholar kind) → None
    (rely on the next backfill pass).  We don't try to derive a key from
    the slug here — the slug is a presentation detail and the catalog
    already has rows whose slug is an old address.
    """
    ek = meta.get("entity_key")
    if isinstance(ek, str) and ek.strip():
        return ek.strip()
    if kind == WikiKind.scholar.value:
        aid = meta.get("openalex_id")
        if isinstance(aid, str) and aid.startswith("A"):
            return f"scholar:{aid}"
    return None


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

    Routes each outbound link through :func:`_links.resolve_target` so a page
    that points at a now-redirected slug still counts as a backlink to the
    canonical page.  Returns the number of pages touched.
    """
    _links.clear_resolve_cache()
    pages = session.exec(select(WikiPage)).all()
    counts: dict[int, int] = {}
    for p in pages:
        if p.redirects_to is not None:
            # Redirect shells have a stub body; their outbound links would
            # only point at the canonical they're redirecting to, so we
            # skip them to avoid self-counting.
            continue
        for href in p.links_out or []:
            tpage = _links.resolve_target(session, p.path, href)
            if tpage is not None and tpage.id != p.id:
                counts[tpage.id] = counts.get(tpage.id, 0) + 1
    touched = 0
    for p in pages:
        new_count = counts.get(p.id, 0)
        if p.links_in_count != new_count:
            p.links_in_count = new_count
            touched += 1
    session.commit()
    return touched


# Source-page kinds whose ``links_out`` we prune.  Scholar pages are left
# alone: their outbound links may include user-authored references to
# concepts / questions that have not (yet) compiled, and dropping those
# would silently break hand-curated notes.  Concept / question pages, by
# contrast, are auto-generated; stale links there are noise.
_PRUNE_KINDS = {WikiKind.concept.value, WikiKind.question.value}


def prune_dead_links(session: Session) -> int:
    """Drop ``links_out`` entries that no longer resolve to a live page.

    Only concept/question source pages are touched (see :data:`_PRUNE_KINDS`).
    For each remaining link, we ask :func:`_links.resolve_target`; a result
    of ``None`` (file gone, row missing, or broken redirect chain) means the
    link is dead.  Returns the number of pages that had at least one link
    pruned.
    """
    _links.clear_resolve_cache()
    pages = session.exec(
        select(WikiPage).where(WikiPage.kind.in_(list(_PRUNE_KINDS)))
    ).all()
    touched = 0
    for p in pages:
        if p.redirects_to is not None or not p.links_out:
            continue
        kept: list[str] = []
        for href in p.links_out:
            tpage = _links.resolve_target(session, p.path, href)
            if tpage is None:
                # Dead link: file gone, row missing, or broken redirect
                # chain.  Drop it from the page's outbound set so the
                # rendered Markdown does not contain a broken wikilink.
                continue
            kept.append(href)
        if len(kept) != len(p.links_out):
            p.links_out = kept
            session.add(p)
            touched += 1
    if touched:
        session.commit()
    return touched
