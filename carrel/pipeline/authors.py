"""Author disambiguation via OpenAlex Work backfill.

Many in-library papers (especially those sourced from Semantic Scholar) store
authors with abbreviated names and no OpenAlex Author ID — e.g. ``{"name":
"G. Chan"}``. Without an A-ID, the Scholars page can only group by exact name
string, so the same person ("G. Chan" vs "Garnet Kin-Lic Chan") is split across
several scholar entries.

This pipeline resolves each such paper's canonical authorship from OpenAlex
(using its DOI / arXiv ID) and writes the authoritative A-ID, display name, and
affiliation back into ``paper.authors``. It is:

  * **Idempotent** — papers whose authors all have A-IDs are skipped.
  * **Authoritative** — no fuzzy name heuristic; we only fill what OpenAlex
    returns for the exact DOI/arXiv match, so two different people sharing
    initials can never be merged incorrectly.
  * **Non-fatal** — a lookup failure leaves the paper's authors unchanged.
  * **Polite** — one request per paper, with a short sleep between them.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from sqlmodel import Session, select

from carrel.models import Paper
from carrel.sources import openalex_client as oa

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]

# Polite pacing between OpenAlex requests. The doi: form is cheap, but a batch
# of ~70 papers without any delay can still trip the rate limiter.
_REQUEST_SLEEP = 0.4


def _has_missing_author_id(paper: Paper) -> bool:
    """True if at least one author record lacks an OpenAlex Author ID."""
    authors = paper.authors or []
    if not authors:
        return False
    return any(
        not str(a.get("openalex_author_id") or "").strip()
        for a in authors
        if isinstance(a, dict)
    )


def select_pending(session: Session, limit: int = 100) -> list[Paper]:
    """In-library papers with an unresolved author and a DOI or arXiv ID."""
    papers = session.exec(
        select(Paper).where(
            Paper.in_library.is_(True),
            Paper.discarded.is_(False),
        )
    ).all()
    out = [
        p
        for p in papers
        if (p.doi or p.arxiv_id) and _has_missing_author_id(p)
    ]
    # Most-cited first: these resolve the most-visible scholars first.
    out.sort(key=lambda p: p.citation_count or 0, reverse=True)
    return out[:limit]


def _merge_authors(
    existing: list[dict[str, Any]], canonical: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """Merge canonical OpenAlex authorship into the stored author list.

    When both lists have the same length, align by position and fill in the
    A-ID / affiliation / canonical name while preserving author order (which
    typically matches between S2/arXiv and OpenAlex). When lengths differ,
    replace wholesale with OpenAlex's ordering — this is rarer and safer than
    a guessy alignment.

    Returns ``(merged_authors, replaced)``.
    """
    same_len = len(existing) == len(canonical) and len(canonical) > 0
    if not same_len:
        return [dict(a) for a in canonical], True

    merged: list[dict[str, Any]] = []
    for old, new in zip(existing, canonical, strict=True):
        if not isinstance(old, dict):
            old = {}
        merged.append(
            {
                "name": new.get("name") or old.get("name") or "",
                "openalex_author_id": new.get("openalex_author_id") or "",
                "affiliation": new.get("affiliation") or old.get("affiliation"),
            }
        )
    return merged, False


def backfill_paper(
    session: Session,
    paper: Paper,
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Resolve one paper's authors from OpenAlex and persist them.

    Returns a status string: ``"filled"`` | ``"skipped"`` | ``"failed"``.
    """
    if not (paper.doi or paper.arxiv_id):
        return "skipped"
    if not force and not _has_missing_author_id(paper):
        return "skipped"

    if on_progress:
        on_progress(
            {
                "paper_id": paper.id,
                "paper_title": paper.title,
                "stage": "fetch",
                "detail": f"Looking up {paper.doi or paper.arxiv_id}",
            }
        )

    canonical = oa.fetch_work_authors(
        paper.doi, paper.arxiv_id, title_hint=paper.title
    )
    if not canonical:
        logger.info("author backfill: no OpenAlex work for %s", paper.id)
        return "failed"

    existing = paper.authors or []
    merged, replaced = _merge_authors(existing, canonical)
    paper.authors = merged
    session.add(paper)
    session.commit()

    if replaced:
        logger.info(
            "author backfill: %s author count mismatch (%d vs %d), replaced",
            paper.id,
            len(existing),
            len(canonical),
        )
    return "filled"


def backfill_batch(
    session: Session,
    limit: int = 100,
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Run backfill over all pending papers. Returns counts by status."""
    targets = select_pending(session, limit=limit)
    counts = {"filled": 0, "skipped": 0, "failed": 0, "total": len(targets)}
    for i, paper in enumerate(targets):
        status = backfill_paper(
            session, paper, force=force, on_progress=on_progress
        )
        counts[status] += 1
        if on_progress:
            on_progress(
                {
                    "stage": "progress",
                    "detail": (
                        f"{i + 1}/{len(targets)} — "
                        f"{paper.title[:60]}"
                    ),
                    **counts,
                }
            )
        time.sleep(_REQUEST_SLEEP)
    return counts
