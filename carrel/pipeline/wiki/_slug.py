"""Filesystem slug helpers for wiki pages.

Slugs are ASCII, lowercase, with runs of non-alphanumeric characters collapsed
to a single dash. Scholar pages prefer the bare OpenAlex A-ID (e.g.
``A5013214678``); name-only scholars get a ``name--jane-doe`` prefix so they
can never collide with an A-ID (which is always ``A`` followed by digits).
"""
from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_AID = re.compile(r"^A\d+$")


def slugify(text: str) -> str:
    """Lowercase ASCII slug: ``"RAG is great!" -> "rag-is-great"``."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower().strip()
    slug = _NON_ALNUM.sub("-", normalized)
    return slug.strip("-") or "untitled"


def scholar_slug(aid: str | None, name: str | None) -> str:
    """Slug for a scholar page: A-ID when known, else ``name--<safe-name>``."""
    if aid and _AID.match(aid.strip()):
        return aid.strip()
    base = slugify(name or "unknown")
    return f"name--{base}"


def page_path(kind: str, slug: str) -> str:
    """Storage-root-relative path, e.g. ``wiki/scholars/A5013....md``."""
    return f"wiki/{kind}s/{slug}.md"
