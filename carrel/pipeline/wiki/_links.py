"""Internal wiki dual-link extraction and resolution.

Internal references use a dual format that both Obsidian and a plain Markdown
viewer understand: ``[[Display label]](../concepts/foo.md)`` — Obsidian sees
the ``[[...]]`` wikilink, standard Markdown follows the ``(...)`` relative
URL. Our rehype plugin (frontend) turns these into client-side routes.

External links (http/https, ``/papers/...``, mailto, anchors) are left alone.
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
from collections.abc import Callable

from sqlmodel import Session, select

from carrel.models import WikiPage

logger = logging.getLogger(__name__)

# [[Label]](relative/path.md) — two closing brackets before the paren.
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]\(([^)]+)\)")

_KIND_DIRS = {"concepts": "concept", "scholars": "scholar", "questions": "question"}

# How many hops to follow when chasing a redirect chain. 4 is generous for
# legitimate rename sequences but small enough to bound cycles introduced by
# hand-edited frontmatter or buggy auto-merges.
_MAX_REDIRECT_HOPS = 4

# Per-process cache for resolve_target. Cleared by the reindex entry points
# (``reindex_wiki`` / ``recompute_backlinks``) so a stale mapping after an
# alias merge doesn't leak across runs.
_resolve_cache: dict[tuple[str, str], int | None] = {}


def clear_resolve_cache() -> None:
    _resolve_cache.clear()


def extract_wikilinks(md: str) -> list[tuple[str, str]]:
    """Return ``[(display, href), ...]`` for every internal dual-link."""
    return [(m.group(1).strip(), m.group(2).strip()) for m in _WIKILINK_RE.finditer(md)]


def is_internal(href: str) -> bool:
    h = href.strip()
    if not h:
        return False
    if h.startswith(("#", "http://", "https://", "mailto:", "/papers/")):
        return False
    return h.endswith(".md")


def resolve_link(from_path: str, href: str) -> tuple[str, str] | None:
    """Resolve an internal href into ``(kind, slug)`` if it targets a wiki page.

    ``from_path`` is storage-root-relative (e.g. ``wiki/scholars/A.md``).
    Returns None for external links or paths outside ``wiki/<kind>/``.
    """
    h = href.split("#", 1)[0].strip()
    if not is_internal(h):
        return None
    from_dir = posixpath.dirname(from_path.replace(os.sep, "/"))
    target = posixpath.normpath(posixpath.join(from_dir, h.replace(os.sep, "/")))
    parts = target.split("/")
    # Expect wiki/<kind-dir>/<slug>.md
    if len(parts) >= 3 and parts[0] == "wiki":
        kind = _KIND_DIRS.get(parts[1])
        if kind is not None and parts[-1].endswith(".md"):
            slug = parts[-1][:-3]
            return kind, slug
    return None


def _live_page(session: Session, kind: str, slug: str) -> WikiPage | None:
    """The non-redirect page for ``(kind, slug)``; None if missing or a shell."""
    return session.exec(
        select(WikiPage).where(
            WikiPage.kind == kind,
            WikiPage.slug == slug,
            WikiPage.redirects_to.is_(None),
        )
    ).first()


def resolve_target(
    session: Session,
    from_path: str,
    href: str,
    *,
    page_resolver: Callable[[str, str], WikiPage | None] | None = None,
) -> WikiPage | None:
    """Resolve a wiki link to its *live* target page, following redirect chains.

    ``page_resolver(kind, slug)`` defaults to :func:`_live_page` (a fresh DB
    lookup).  Inject a stub for tests; a cache may be added later at this seam
    without touching call sites.

    Returns the canonical page (last non-redirect row in the chain) or
    ``None`` if the link is external, points at a missing page, or chains
    beyond ``_MAX_REDIRECT_HOPS`` (the latter logs a warning so hand-edited
    cycles don't fail silently).
    """
    resolved = resolve_link(from_path, href)
    if resolved is None:
        return None
    kind, slug = resolved
    cache_key = (kind, slug)
    if cache_key in _resolve_cache:
        target_id = _resolve_cache[cache_key]
        if target_id is None:
            return None
        return session.get(WikiPage, target_id)

    # Default resolver finds the row at ``(kind, slug)`` whether or not it's
    # a redirect shell — the loop below follows `redirects_to` until it
    # lands on a live row.  ``_live_page`` would short-circuit shells and
    # drop the chain; we want to start the chain from the shell itself.
    def _any_page(k: str, s: str):
        return session.exec(
            select(WikiPage).where(
                WikiPage.kind == k,
                WikiPage.slug == s,
            )
        ).first()

    resolver = page_resolver or _any_page
    page = resolver(kind, slug)
    if page is None:
        _resolve_cache[cache_key] = None
        return None

    # Follow redirects, but skip pages whose entity_key already equals the
    # target (caller already passed a canonical row).
    for _hop in range(_MAX_REDIRECT_HOPS):
        if page.redirects_to is None:
            break
        target = session.exec(
            select(WikiPage).where(
                WikiPage.entity_key == page.redirects_to,
                WikiPage.redirects_to.is_(None),
            )
        ).first()
        if target is None:
            logger.warning(
                "wiki resolve_target: broken redirect from %s/%s -> %s (target missing)",
                kind, slug, page.redirects_to,
            )
            # The source page itself is a redirect shell (or its first
            # hop lands on a shell because the target entity was deleted).
            # Return None — backlinks must not count a shell as a target.
            _resolve_cache[cache_key] = None
            return None
        page = target
    else:
        logger.warning(
            "wiki resolve_target: redirect chain from %s/%s exceeded %d hops; "
            "returning last page seen (entity_key=%s)",
            kind, slug, _MAX_REDIRECT_HOPS, page.entity_key,
        )

    _resolve_cache[cache_key] = page.id
    return page
