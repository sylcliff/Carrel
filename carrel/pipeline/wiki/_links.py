"""Internal wiki dual-link extraction and resolution.

Internal references use a dual format that both Obsidian and a plain Markdown
viewer understand: ``[[Display label]](../concepts/foo.md)`` — Obsidian sees
the ``[[...]]`` wikilink, standard Markdown follows the ``(...)`` relative
URL. Our rehype plugin (frontend) turns these into client-side routes.

External links (http/https, ``/papers/...``, mailto, anchors) are left alone.
"""
from __future__ import annotations

import os
import posixpath
import re

# [[Label]](relative/path.md) — two closing brackets before the paren.
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]\(([^)]+)\)")

_KIND_DIRS = {"concepts": "concept", "scholars": "scholar", "questions": "question"}


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
