"""Parse/dump the YAML frontmatter of a wiki page.

A page is ``---\\\\n<yaml>\\\\n---\\\\n<body>``. We split on the first two ``---``
lines rather than pull in a full markdown parser. The frontmatter is a flat-ish
dict written by the compiler; hand edits are tolerated (a malformed block
causes :func:`parse` to return no metadata, leaving the body intact).
"""
from __future__ import annotations

from typing import Any

import yaml

_FENCE = "---"


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter_dict, body_without_frontmatter)``.

    If no frontmatter is present, returns ``({}, text)``.
    """
    if not text.startswith(_FENCE):
        return {}, text
    # Accept an optional leading newline after the opening fence.
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            end = i
            break
    if end is None:
        return {}, text
    block = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    if body.startswith("\n"):
        body = body[1:]
    try:
        data = yaml.safe_load(block) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, body


def dump(meta: dict[str, Any], body: str) -> str:
    """Render ``meta`` as frontmatter followed by ``body``."""
    block = yaml.safe_dump(
        meta, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).strip()
    return f"{_FENCE}\n{block}\n{_FENCE}\n\n{body.lstrip()}"
