"""Paper metadata helpers shared by every LLM-driven pipeline.

Three pipelines (``summarize``, ``paper_extract``, ``paper_card``) all
need to render the same two strings from a :class:`Paper` row for the
LLM prompt:

- the comma-separated author list
- the venue-and-date line

Before this module each pipeline kept its own byte-identical
``_authors_string`` / ``_venue_date_string`` helpers, so any change to
the author JSON shape or the venue-date format had to be made in three
places. The functions now live here once.

Also exposes :data:`LLM_EXTRACT_PER_SECTION_CAP` — the per-section char
cap that :mod:`carrel.pipeline._llm_extract` passes into
:func:`carrel.pipeline._section_picker.prepare_picker_input`. The wiki
compilers have their own per-pipeline cap (much larger; see their
``budget_chars`` calls) and don't go through this constant.

Finally exposes :data:`USER_PROMPT_TEMPLATE` — the user-side prompt
skeleton shared by all three extraction pipelines (``summarize`` /
``paper_extract`` / ``paper_card``). Placeholders: ``{title}``,
``{authors}``, ``{venue_date}``, ``{abstract}``, ``{numbered_sections}``.
Each pipeline builds the body via :func:`build_user_prompt`.
"""
from __future__ import annotations

from carrel.models import Paper


# Cap on LLM input per single section in the three extraction pipelines
# (``summarize``, ``paper_extract``, ``paper_card``). The picker fills
# the budget with the highest-priority sections (method → results →
# conclusion → intro) and drops references / acknowledgments /
# supplementary / appendix / funding boilerplate, so 600 is plenty
# when sections are short and tight enough to fail gracefully on a
# paper with no recognisable headings. Wiki compiles use a different
# (larger) per-section cap via ``prepare_picker_input``'s own default.
LLM_EXTRACT_PER_SECTION_CAP = 600


# User-side prompt skeleton shared by the three extraction pipelines
# (summarize / paper_extract / paper_card). Each pipeline passes its
# own paper row + numbered-sections body; the surrounding frame is
# identical so a new field (e.g. language, published_at) is a one-line
# change here rather than three. The system prompts stay per-pipeline
# because the JSON schema and tone differ.
USER_PROMPT_TEMPLATE = (
    "Title: {title}\n"
    "Authors: {authors}\n"
    "Venue/date: {venue_date}\n\n"
    "Abstract:\n{abstract}\n\n"
    "Selected paper sections (parsed from PDF; may contain OCR noise; "
    "references/acknowledgments/supplementary dropped):\n\n"
    "{numbered_sections}\n\n"
    "Return the JSON object now."
)


def authors_string(paper: Paper) -> str:
    """Comma-joined author display names. Empty string when none."""
    if not paper.authors:
        return ""
    names = [
        a.get("name", "")
        for a in paper.authors
        if isinstance(a, dict) and a.get("name")
    ]
    return ", ".join(names)


def venue_date_string(paper: Paper) -> str:
    """Venue and publication date joined by a middot. Empty when both blank."""
    bits = [paper.venue or ""]
    if paper.publication_date:
        bits.append(str(paper.publication_date))
    return " · ".join(b for b in bits if b)


def build_user_prompt(paper: Paper, numbered_sections: str) -> str:
    """Render the shared extraction user-prompt for a paper.

    The pipeline-specific bits (system prompt + JSON schema) live with
    each pipeline; the per-paper framing lives here so a new field on
    the paper side is one edit instead of three. ``abstract`` is taken
    from ``paper.abstract`` (empty string when None).
    """
    return USER_PROMPT_TEMPLATE.format(
        title=paper.title or "",
        authors=authors_string(paper),
        venue_date=venue_date_string(paper),
        abstract=paper.abstract or "",
        numbered_sections=numbered_sections,
    )
