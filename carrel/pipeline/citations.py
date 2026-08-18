"""Citation enrichment pipeline (Semantic Scholar).

For each paper we fetch the citation count, influential/reference counts, and a
capped list of citing papers, then persist them on the ``Paper`` row. This
module is synchronous and mirrors :mod:`carrel.pipeline.process`:

  - :func:`enrich_paper` enriches one paper and reports progress via a callback
    shaped like process.py's (a dict with ``stage`` / ``detail``).
  - :func:`enrich_papers` walks a list, sleeping between calls to stay polite.
  - :func:`select_stale` picks papers never enriched (for a first backfill).

Failures are soft: a network/rate-limit error is logged and re-raised so the
per-paper Job can be marked failed, but callers that batch many papers (sync)
catch and continue so one bad lookup never aborts a sync run.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from sqlmodel import Session, select

from carrel.config import CarrelYAML
from carrel.models import Paper
from carrel.sources import semanticscholar_client as s2

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]


def enrich_paper(
    session: Session,
    cfg: CarrelYAML,
    paper_id: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> bool:
    """Look up one paper on S2 and persist its citation data.

    Returns ``True`` when citation data was written (found or explicitly
    empty), ``False`` when the paper has no resolvable identifier. A paper S2
    cannot find still gets its ``citations_updated_at`` stamped so we don't
    retry it on every sync.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        return False

    def _progress(detail: str, **extra: object) -> None:
        if on_progress is not None:
            on_progress({"stage": "citations", "detail": detail, **extra})

    limit = cfg.semantic_scholar.citations_limit

    if not (paper.s2_paper_id or paper.doi or paper.arxiv_id):
        logger.info("paper %s has no DOI/arXiv/S2 id; skipping citations", paper.id)
        _progress("No identifier to look up", paper_id=paper.id)
        paper.citations_updated_at = datetime.now(UTC)
        session.add(paper)
        session.commit()
        return False

    _progress("Querying Semantic Scholar…", paper_id=paper.id)
    result = s2.fetch_citations(
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
        s2_id=paper.s2_paper_id,
        limit=limit,
    )

    now = datetime.now(UTC)
    if result is None:
        # S2 has no record; stamp so we don't keep retrying.
        paper.citations_updated_at = now
        session.add(paper)
        session.commit()
        _progress("Not found on Semantic Scholar", paper_id=paper.id)
        return False

    paper.s2_paper_id = result.s2_paper_id or paper.s2_paper_id
    paper.citation_count = result.citation_count
    paper.influential_citation_count = result.influential_count
    paper.reference_count = result.reference_count
    paper.citing_papers = result.citing_papers
    paper.citations_updated_at = now
    session.add(paper)
    session.commit()

    n = len(result.citing_papers)
    _progress(
        f"{result.citation_count if result.citation_count is not None else n} citations",
        paper_id=paper.id,
    )
    return True


def enrich_papers(
    session: Session,
    cfg: CarrelYAML,
    paper_ids: list[str],
    *,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Enrich many papers, one at a time, with a courtesy delay between them.

    Returns counters ``{enriched, failed, skipped}``. A failure on one paper is
    logged and counted but does not stop the batch.
    """
    enriched = failed = skipped = 0
    delay = cfg.semantic_scholar.delay_between_requests_seconds
    total = len(paper_ids)

    for idx, pid in enumerate(paper_ids):
        if on_progress is not None:
            on_progress({
                "stage": "citations",
                "detail": f"Looking up citations ({idx + 1}/{total})…",
                "paper_id": pid,
            })
        try:
            if enrich_paper(session, cfg, pid, on_progress=on_progress):
                enriched += 1
            else:
                skipped += 1
        except Exception as e:  # noqa: BLE001 - batch must continue
            logger.warning("citation enrichment failed for %s: %s", pid, e)
            failed += 1
        if delay and idx < total - 1:
            time.sleep(delay)

    return {"enriched": enriched, "failed": failed, "skipped": skipped}


def select_stale(session: Session, limit: int = 50) -> list[Paper]:
    """Return papers that have never had citations fetched (oldest first)."""
    stmt = (
        select(Paper)
        .where(Paper.citations_updated_at.is_(None))
        .order_by(Paper.created_at.asc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())
