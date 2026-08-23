"""Shared name helpers for the wiki layer.

Lives at the wiki package root (not inside ``_scholars_agg``) so that
:mod:`carrel.pipeline.wiki._slug` and the backfill in :mod:`carrel.db` can
both import it without a cycle.  ``_scholars_agg`` is the one place that
*produces* author keys from raw paper metadata, so it depends on this
module; the reverse direction must stay acyclic.
"""
from __future__ import annotations

import re

# Collapse runs of whitespace, ASCII dots and hyphens so surface forms
# like "He-Li", "He  Li", "He.Li", "He/Li" all key to the same scholar.
# (The slug layer adds back its own dashes from the normalized form.)
_WS_DOT = re.compile(r"[.\s\-/]+")


def normalize_name(name: str | None) -> str:
    """Return a stable lowercase form suitable for keys and slug inputs.

    Display names shown to the user still come from the most-common raw
    spelling (see :func:`carrel.pipeline.wiki._scholars_agg.aggregate`);
    only the *key* and *slug* use the normalized form.
    """
    if not name:
        return ""
    return _WS_DOT.sub(" ", name.strip()).lower()
