"""Protect user-authored sections across recompilations.

A compiled page reserves a ``<section data-user="true">...</section>`` block
for anything the user types by hand. The compiler owns everything outside that
block; on recompile it extracts the existing user section verbatim and splices
it into the freshly generated document, so manual edits survive while the
synthesized prose updates.
"""
from __future__ import annotations

import re

_USER_SECTION_RE = re.compile(
    r"(<section\s+data-user=\"true\"[^>]*>.*?</section>)",
    re.DOTALL,
)
_OPEN_RE = re.compile(r"<section\s+data-user=\"true\"[^>]*>")
_CLOSE_RE = re.compile(r"</section>")

# Empty placeholder used on first compile; a comment explains what it is when
# the file is opened in a plain editor.
EMPTY_USER_SECTION = (
    '<section data-user="true">\n'
    "<!-- Your notes on this page. The compiler preserves everything inside "
    "this section. -->\n"
    "</section>"
)


def extract_user_section(text: str) -> str | None:
    """Return the first user-section markup verbatim, or None if absent."""
    m = _USER_SECTION_RE.search(text)
    return m.group(1) if m else None


def _insert_section(text: str, section: str) -> str:
    """Insert ``section`` right after the first H1, or at the top if none.

    Assumes the caller has already confirmed no user section exists in ``text``.
    """
    insertion = f"\n\n{section}\n"
    # Place after the first "# Title" line.
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return "\n".join(lines[: i + 1]) + insertion + "\n".join(lines[i + 1 :])
    return section + "\n\n" + text


def ensure_user_section(text: str) -> str:
    """Insert an empty user section right after the first H1 if none exists.

    If there is no H1, the section is placed at the very top (after any
    frontmatter — callers pass a body with frontmatter already stripped).
    """
    if extract_user_section(text) is not None:
        return text
    return _insert_section(text, EMPTY_USER_SECTION)


def protect_user_section(old_text: str | None, new_text: str) -> str:
    """Splice the user section from ``old_text`` into ``new_text``.

    If ``new_text`` already carries a (placeholder) section it is replaced by the
    old one; otherwise the old section is inserted after the H1. When
    ``old_text`` has no section (or is None), an empty placeholder is ensured.
    The preserved/empty section is always used — a freshly rendered body that
    contains no section must never clobber existing user notes.
    """
    old_section = extract_user_section(old_text) if old_text else None
    section = old_section or EMPTY_USER_SECTION

    if _USER_SECTION_RE.search(new_text):
        return _USER_SECTION_RE.sub(lambda _m: section, new_text, count=1)
    return _insert_section(new_text, section)
