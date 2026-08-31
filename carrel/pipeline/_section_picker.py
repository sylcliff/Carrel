"""Intent-aware section picker for LLM input.

Replaces the dumb "first 2 + last 2 sections" head/tail trim with a
budgeted priority fill.  The LLM gets a budgeted slice of the paper
spanning the sections most likely to carry the fields we want extracted
(method/results/conclusion), with explicit section labels so it can
attribute each fact to the right place.

Design points:
  * Pure-Python regex over the existing :func:`carrel.chunking.
    split_by_heading` output — no new deps, no token counters.
  * Two passes of regex matching: ``DROP`` (throw away noise sections
    like references / acknowledgments / supplementary) and
    ``PRIORITY_RULES`` (assign an integer priority; lower wins).
  * Budget fill: walk priorities in order, cap each section so one
    monster Results block can't blow the budget, stop when filled.
  * Numbered output: ``## [1] Methods``, ``## [2] Results`` … so the
    LLM can ground each field to a specific section in the prompt.
  * Falls back to the existing head+tail char window when the paper
    has no ATX headings (typical for OCR-noisy short papers).

This is shared by every LLM-driven per-paper pipeline that wants more
than the opening + closing of a paper.  Right now: paper_card.
"""
from __future__ import annotations

import re
from collections.abc import Iterable


# Sections to throw away: they carry the bibliography text, funding
# boilerplate, or appendix material that distracts the LLM.  The
# pattern matches against any segment of the heading path so that
# "Supplementary / Methods" is dropped (parent matches) while a bare
# "Methods" is kept.
_DROP_PATTERNS: tuple[str, ...] = (
    r"\breferences?\b",
    r"\bbibliograph(?:y|ies)\b",
    r"\backnowledg(?:e?ments?|ments)\b",
    r"\bsupplement(?:ary|ary\s+material|ary\s+information|ary\s+info)?\b",
    r"\bappendi(?:ces|x)\b",
    r"\bauthor\s+(?:contributions?|information|biographies)\b",
    r"\bfunding(?:\s+(?:information|statement|acknowledg))?\b",
    r"\bdata\s+availability\b",
    r"\bconflicts?\s+of\s+interest\b",
    r"\bdeclaration(?:s)?\b",
    r"\babbreviations?\b",
    r"\bnotation\b",
    r"\bglossary\b",
)
_DROP_RE = re.compile("|".join(_DROP_PATTERNS), re.IGNORECASE)


# Priority rules: lower number wins.  A section matches if the regex
# hits the leaf segment (last "/"-separated part) of its heading path.
# ``label`` is a short noun used in the numbered output (e.g. "[1]
# Methods") so the LLM can see what each block is even when the
# original heading was generic ("## 3") or absent on a sub-section.
# Order in this tuple is the tiebreaker for sections at the same
# priority.
_PRIORITY_RULES: tuple[tuple[int, re.Pattern[str], str], ...] = (
    (1, re.compile(
        r"\b(?:method|methods|methodology|approach|model|"
        r"architecture|framework|algorithm|setup|implementation|"
        r"design|proposed\s+(?:method|approach|model|framework|"
        r"architecture|implementation)|"
        r"our\s+(?:approach|method|model|framework|implementation))\b",
        re.IGNORECASE,
    ), "Method"),
    (2, re.compile(
        r"\b(?:result|results|experiment|experiments|"
        r"evaluation|performance|ablation|study|empirical|"
        r"analysis|findings?|classification|benchmark|"
        r"experiments?\s+and\s+results?)\b",
        re.IGNORECASE,
    ), "Results"),
    (3, re.compile(
        r"\b(?:conclusion|conclusions|discussion|"
        r"limitation|limitations|future\s+work|future\s+direction|"
        r"summary)\b",
        re.IGNORECASE,
    ), "Conclusion"),
    (4, re.compile(
        r"\b(?:introduction|background|related\s+work|related\s+research|"
        r"motivation|overview|preliminaries|problem\s+statement|"
        r"problem\s+formulation|objectives?|contributions?)\b",
        re.IGNORECASE,
    ), "Intro"),
)


def _is_drop(heading: str) -> bool:
    """True if any segment of the heading path matches a DROP pattern.

    Matching against any segment (not just the leaf) means a "Methods"
    inside "Supplementary / Methods" gets dropped — the parent
    "Supplementary" carries the intent.
    """
    if not heading:
        return False
    for segment in heading.split("/"):
        if _DROP_RE.search(segment):
            return True
    return False


def _classify(heading: str) -> tuple[int, str] | None:
    """Return ``(priority, label)`` for a section, or None if unranked.

    Walks the heading path from leaf outward so that a sub-section
    under "## 3. Methods" classifies as Method even when the leaf
    itself ("3.1 Hamiltonian Encoding") has no keyword.  A section
    that doesn't match any rule is kept at priority 99 so a paper
    with non-standard headings doesn't lose content silently.
    """
    if not heading:
        return (99, "Body")
    segments = [s.strip() for s in heading.split("/") if s.strip()]
    if not segments:
        return (99, "Body")
    # Walk leaf-first so a more specific match (rare) wins over the
    # parent — but in practice parents usually carry the intent.
    for segment in reversed(segments):
        for prio, rx, label in _PRIORITY_RULES:
            if rx.search(segment):
                return (prio, label)
    # No keyword anywhere in the path: unranked.  Label is the
    # leaf so the LLM still sees what the section was called.
    return (99, segments[-1])


def _strip_image_lines(md: str) -> str:
    """Drop lines that start with ``!`` (MinerU image markup)."""
    return "\n".join(
        line for line in md.splitlines() if not line.lstrip().startswith("!")
    )


def select_sections(
    md: str,
    *,
    budget_chars: int,
    per_section_cap: int = 4_000,
) -> list[tuple[int, int, str, str]]:
    """Pick sections to send to the LLM under a character budget.

    Returns a list of ``(priority, order, label, body)`` tuples, sorted
    in **document order** so the LLM reads the paper in its natural
    sequence (Abstract → Intro → Methods → Results → Conclusion) and
    can ground each fact to the surrounding context.  ``priority`` is
    preserved on each tuple so callers / tests can still inspect it;
    ``order`` is the section's index in the original document.

    A section is dropped if:
      * its heading path matches a DROP pattern, or
      * its body is empty after stripping image lines.

    A section is truncated to ``per_section_cap`` chars so one
    monster Results block can't eat the whole budget.

    Budget allocation: candidates are walked in **priority order**
    (Methods before Conclusions), so a tight budget first sacrifices
    low-priority sections (Intro, unrelated Body) before the
    high-priority ones.  The picked list is then re-sorted by
    document ``order`` for output.
    """
    if not md or not md.strip():
        return []
    md = _strip_image_lines(md)

    # Lazy import: chunking pulls a small amount of regex/heuristics
    # and we don't want to pay the import on every LLM-test import.
    from carrel.chunking import split_by_heading

    sections = split_by_heading(md)

    # Iterate in **priority order**, not document order, so a paper
    # whose doc leads with an Introduction (priority 4) doesn't
    # starve later Methods / Results / Conclusion (priority 1-3) of
    # budget.  Within a priority bucket, document order is the
    # stable tiebreaker.
    candidates: list[tuple[int, int, str, str]] = []
    for order, (heading, body) in enumerate(sections):
        if _is_drop(heading):
            continue
        if not body or not body.strip():
            continue
        # _classify never returns None — it falls back to (99, leaf-or-"Body")
        # for unranked sections. Direct unpack.
        prio, label = _classify(heading)
        candidates.append((prio, order, label, body.strip()))

    candidates.sort(key=lambda x: (x[0], x[1]))

    picked: list[tuple[int, int, str, str]] = []
    used = 0
    for prio, order, label, body in candidates:
        chunk = body
        if len(chunk) > per_section_cap:
            chunk = chunk[:per_section_cap].rstrip() + "\n…"
        if used + len(chunk) > budget_chars:
            remaining = budget_chars - used
            if remaining <= 200:  # not worth a half-section
                break
            chunk = chunk[:remaining].rstrip() + "\n…"
        picked.append((prio, order, label, chunk))
        used += len(chunk)
        if used >= budget_chars:
            break

    # Re-sort in document order for output: the LLM reads the paper
    # in its natural sequence, so "## [3] Method" (from §2) should
    # come before "## [6] Results" (from §3), not be grouped by type.
    picked.sort(key=lambda x: x[1])
    return picked


def render_numbered(
    picked: Iterable[tuple[int, int, str, str]],
) -> str:
    """Render a ``select_sections`` result as numbered markdown blocks.

    Output format::

        ## [1] Methods
        <body>

        ## [2] Results
        <body>

    The bracketed ordinal indexes the picked list in its current
    order (document order after :func:`select_sections` returns) and
    is therefore stable across re-runs.  ``label`` is a fallback used
    when the original heading is empty or non-descriptive ("Section 3"
    → "[3] Method").
    """
    out: list[str] = []
    for i, (_prio, _order, label, body) in enumerate(picked, start=1):
        out.append(f"## [{i}] {label}")
        out.append(body)
    return "\n\n".join(out).strip()


def prepare_picker_input(
    md: str,
    *,
    budget_chars: int,
    per_section_cap: int = 4_000,
) -> str:
    """Convenience: strip + select + render in one call.

    Returns the empty string when the paper has no extractable
    content; callers can use that to short-circuit.  Falls back to a
    head+tail char window when the picker yields nothing (e.g. a
    paper with no recognised headings) so the LLM still gets *some*
    context.
    """
    if not md or not md.strip():
        return ""
    # Don't pre-strip here — ``select_sections`` already calls
    # ``_strip_image_lines`` internally.  An extra pass would double
    # the O(n) work for every LLM call (paper_card, paper_extract,
    # summarize, chat-fallback) without changing the output.  The
    # fallback path below also strips to keep image-base64 out of the
    # head+tail window the LLM sees.
    picked = select_sections(
        md, budget_chars=budget_chars, per_section_cap=per_section_cap
    )
    if picked:
        return render_numbered(picked)
    # Fallback: no usable sections → first + last char windows.  Strip
    # image lines here because this path bypasses ``select_sections``
    # (and therefore the strip it does internally).
    cleaned = _strip_image_lines(md).strip()
    half = max(budget_chars // 2, 500)
    if len(cleaned) <= budget_chars:
        return cleaned
    head = cleaned[:half].rstrip()
    tail = cleaned[-half:].lstrip()
    return f"{head}\n\n…\n\n{tail}"
