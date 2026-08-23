"""Concept aggregation across in-library papers.

A "concept" is a normalized term (e.g. "retrieval-augmented generation") that
the per-paper LLM extraction surfaced from at least one parsed body.  Multiple
papers mentioning the same normalized term collapse into a single concept; the
display form is the most-common surface form across the library.

The wiki concept compiler reads from this module: it gets one
:class:`ConceptCandidate` per term with the backing paper set, and asks
:func:`papers_for_term` to fetch the evidence pack (metadata + tldr/abstract)
it feeds the LLM.

Concept data source is :class:`carrel.models.PaperConcept` (per-paper
extraction rows from the ``paper_extract`` pipeline).  Aggregating from
``papers`` directly would re-litigate the LLM step on every compile.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from carrel.models import Paper, PaperConcept


# Threshold below which a concept is rendered as a stub (no LLM call).
# Lowered from 3 to 1 in M8c: every concept that gets mentioned in any
# paper gets a live LLM-compiled page, even single-paper terms.  Stubs are
# still written when paper_count is 0 (impossible at this point given the
# aggregation requires ≥1 backing paper, but kept for safety).
EVIDENCE_THRESHOLD = 1


@dataclass
class ConceptCandidate:
    """One compiled concept — a normalized term with a paper set and counts.

    Carries the data the compiler needs to decide staleness and to render
    the page: ``term_normalized`` is the dedup key, ``term_display`` is the
    most-common surface form, ``paper_ids`` is the in-library backing set
    (newest first), and ``latest_paper_update`` is the staleness driver.
    """

    term_normalized: str
    term_display: str
    paper_ids: list[str]
    paper_count: int
    latest_paper_update: datetime | None


def aggregate(session: Session) -> list[ConceptCandidate]:
    """Build one :class:`ConceptCandidate` per normalized term.

    Walks ``paper_concepts`` joined to ``papers`` (in-library, not
    discarded), tallies the most-common display form, and returns the
    candidates ordered by paper-count desc then term.  The ordering
    matters for the staleness walker: it processes the most-evidenced
    concepts first.
    """
    rows = session.exec(
        select(PaperConcept, Paper)
        .join(Paper, Paper.id == PaperConcept.paper_id)
        .where(Paper.in_library.is_(True), Paper.discarded.is_(False))
    ).all()

    # normalized -> { display: count, papers: set, latest: max(updated_at) }
    displays: dict[str, Counter] = defaultdict(Counter)
    papers: dict[str, set[str]] = defaultdict(set)
    latest: dict[str, datetime | None] = defaultdict(lambda: None)
    for pc, paper in rows:
        term = pc.term_normalized
        if not term:
            continue
        displays[term][pc.term_display or term] += 1
        papers[term].add(paper.id)
        up = paper.updated_at
        if up is not None and (latest[term] is None or up > latest[term]):
            latest[term] = up

    out: list[ConceptCandidate] = []
    for term, paper_set in papers.items():
        display = displays[term].most_common(1)[0][0]
        out.append(
            ConceptCandidate(
                term_normalized=term,
                term_display=display,
                paper_ids=sorted(paper_set),
                paper_count=len(paper_set),
                latest_paper_update=latest[term],
            )
        )
    out.sort(key=lambda c: (-c.paper_count, c.term_normalized))
    return out


def papers_for_term(session: Session, term_normalized: str) -> list[Paper]:
    """In-library, non-discarded papers that extracted ``term_normalized``.

    Returns at most one row per paper even if a paper contributed several
    ``PaperConcept`` rows for the same term (the join key is unique on
    ``(paper_id, term_normalized)`` so the SET in SQL already de-dupes).
    Sorted newest-first by ``publication_date`` for stable prompt order.
    """
    paper_ids = {
        pc.paper_id
        for pc in session.exec(
            select(PaperConcept).where(PaperConcept.term_normalized == term_normalized)
        ).all()
    }
    if not paper_ids:
        return []
    papers = session.exec(
        select(Paper).where(
            Paper.id.in_(paper_ids),
            Paper.in_library.is_(True),
            Paper.discarded.is_(False),
        )
    ).all()
    papers.sort(
        key=lambda p: (p.publication_date is not None, p.publication_date),
        reverse=True,
    )
    return list(papers)


def enumerate_entities(session: Session) -> list[Any]:
    """Return one :class:`EntityRef` per live concept for the wiki reconciler.

    Mirrors the shape produced by ``_scholars_agg.enumerate_entities`` so the
    same kind-agnostic :func:`carrel.pipeline.wiki._entities.reconcile_kind`
    can fold concepts into the catalog without special cases.  ``entity_key``
    follows the ``concept:<slug>`` convention so a concept page is identified
    by its on-disk address (the slug is the canonical form of the term;
    aliases/redirects are out of scope for v1).
    """
    from carrel.pipeline.wiki._entities import EntityRef
    from carrel.pipeline.wiki._slug import slugify

    out: list[EntityRef] = []
    for c in aggregate(session):
        slug = slugify(c.term_display)
        out.append(
            EntityRef(
                entity_key=f"concept:{slug}",
                kind="concept",
                slug=slug,
                title=c.term_display,
            )
        )
    return out
