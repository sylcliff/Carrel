"""Question aggregation across in-library papers.

A "question" is a normalized question string (e.g. "how can retrieval stay
current with fast-moving knowledge bases") that the per-paper LLM extraction
surfaced from at least one parsed body.  Multiple papers asking the same
normalized question collapse into a single wiki page; the display form is the
most-common surface form across the library.

The wiki question compiler reads from this module: it gets one
:class:`QuestionCandidate` per question with the backing paper set, and asks
:function:`papers_for_question` to fetch the evidence pack (metadata +
tldr/abstract) it feeds the LLM.

Question data source is :class:`carrel.models.PaperQuestion` (per-paper
extraction rows from the ``paper_extract`` pipeline).  Aggregating from
``papers`` directly would re-litigate the LLM step on every compile.

This module is the structural twin of :mod:`carrel.pipeline.wiki._concepts_agg`
— same shape, same ordering, same enumerate-entity contract for the reconciler.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from carrel.models import Paper, PaperQuestion


# Threshold below which a question is rendered as a stub (no LLM call).
# Matches :data:`carrel.pipeline.wiki._concepts_agg.EVIDENCE_THRESHOLD`:
# lowered from 3 to 1 in M8c so any paper-mentioned open question gets a
# live LLM-compiled page.
EVIDENCE_THRESHOLD = 1


@dataclass
class QuestionCandidate:
    """One compiled question — a normalized question with a paper set and counts.

    Carries the data the compiler needs to decide staleness and to render
    the page: ``question_normalized`` is the dedup key, ``question_display``
    is the most-common surface form, ``paper_ids`` is the in-library backing
    set (newest first), and ``latest_paper_update`` is the staleness driver.
    """

    question_normalized: str
    question_display: str
    paper_ids: list[str]
    paper_count: int
    latest_paper_update: datetime | None


def aggregate(session: Session) -> list[QuestionCandidate]:
    """Build one :class:`QuestionCandidate` per normalized question.

    Walks ``paper_questions`` joined to ``papers`` (in-library, not
    discarded), tallies the most-common display form, and returns the
    candidates ordered by paper-count desc then question.  The ordering
    matters for the staleness walker: it processes the most-evidenced
    questions first.
    """
    rows = session.exec(
        select(PaperQuestion, Paper)
        .join(Paper, Paper.id == PaperQuestion.paper_id)
        .where(Paper.in_library.is_(True), Paper.discarded.is_(False))
    ).all()

    # normalized -> { display: count, papers: set, latest: max(updated_at) }
    displays: dict[str, Counter] = defaultdict(Counter)
    papers: dict[str, set[str]] = defaultdict(set)
    latest: dict[str, datetime | None] = defaultdict(lambda: None)
    for pq, paper in rows:
        qn = pq.question_normalized
        if not qn:
            continue
        displays[qn][pq.question_display or qn] += 1
        papers[qn].add(paper.id)
        up = paper.updated_at
        if up is not None and (latest[qn] is None or up > latest[qn]):
            latest[qn] = up

    out: list[QuestionCandidate] = []
    for qn, paper_set in papers.items():
        display = displays[qn].most_common(1)[0][0]
        out.append(
            QuestionCandidate(
                question_normalized=qn,
                question_display=display,
                paper_ids=sorted(paper_set),
                paper_count=len(paper_set),
                latest_paper_update=latest[qn],
            )
        )
    out.sort(key=lambda c: (-c.paper_count, c.question_normalized))
    return out


def papers_for_question(session: Session, question_normalized: str) -> list[Paper]:
    """In-library, non-discarded papers that raised ``question_normalized``.

    Returns at most one row per paper even if a paper contributed several
    ``PaperQuestion`` rows for the same question (the join key is unique on
    ``(paper_id, question_normalized)`` so the SET in SQL already de-dupes).
    Sorted newest-first by ``publication_date`` for stable prompt order.
    """
    paper_ids = {
        pq.paper_id
        for pq in session.exec(
            select(PaperQuestion).where(
                PaperQuestion.question_normalized == question_normalized
            )
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
    """Return one :class:`EntityRef` per live question for the wiki reconciler.

    Mirrors the shape produced by ``_concepts_agg.enumerate_entities`` so the
    same kind-agnostic :func:`carrel.pipeline.wiki._entities.reconcile_kind`
    can fold questions into the catalog without special cases.
    ``entity_key`` follows the ``question:<slug>`` convention so a question
    page is identified by its on-disk address (the slug is the canonical
    form of the question; aliases/redirects are out of scope for v1).
    """
    from carrel.pipeline.wiki._entities import EntityRef
    from carrel.pipeline.wiki._slug import slugify

    out: list[EntityRef] = []
    for c in aggregate(session):
        slug = slugify(c.question_display)
        out.append(
            EntityRef(
                entity_key=f"question:{slug}",
                kind="question",
                slug=slug,
                title=c.question_display,
            )
        )
    return out
