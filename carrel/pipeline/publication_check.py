"""Detect when an arXiv preprint has been formally published.

For arXiv papers older than a threshold (default 180 days) we check Semantic
Scholar (with an OpenAlex fallback) for a non-arXiv journal DOI. When one is
found, the published PDF is fetched via the institutional SSH jump host, kept
*alongside* the arXiv version (``arxiv.pdf`` + ``journal.pdf``), promoted to the
active ``paper.pdf``, and the paper is re-parsed/re-summarized. The journal DOI
and arXiv DOI are both stored and shown on the page.

Safety properties:
  * Detection (metadata) runs even without the remote configured; only the PDF
    fetch needs it.
  * If the journal PDF fetch fails, the arXiv version stays active — we keep
    the recorded ``journal_doi`` (detection succeeded) but do not delete the
    parsed markdown or chunks, so the paper remains readable.
  * Files are swapped in order: download → validate → back up arXiv → overwrite
    paper.pdf → only then clear parse artifacts.

This module is synchronous, like :mod:`carrel.pipeline.process`.
"""
from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlmodel import Session, or_, select

from carrel.config import CarrelYAML, EnvSettings
from carrel.models import Chunk, Paper, PaperStatus
from carrel.pipeline.process import (
    PDF_FILENAME,
    ProcessError,
    paper_paths,
    process_paper,
)
from carrel.sources import arxiv as arxiv_source
from carrel.sources import openalex_client as oa
from carrel.sources import remote_downloader as rd
from carrel.sources import semanticscholar_client as s2

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]

JOURNAL_VENUE_TYPES = {"journal", "conference", "book series", "book"}
ARXIV_FILENAME = "arxiv.pdf"
JOURNAL_FILENAME = "journal.pdf"
JOURNAL_TMP_FILENAME = "journal.pdf.tmp"


@dataclass(slots=True)
class PublicationInfo:
    found: bool
    journal_doi: str | None = None
    venue: str | None = None
    publication_date: str | None = None
    source: str | None = None  # "semanticscholar" | "openalex"
    reason: str | None = None  # why not found, when found=False


# ---------------------------------------------------------------------------
# Age / date helpers
# ---------------------------------------------------------------------------


def _iso_to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _is_old_enough(published: str | None, *, min_age_days: int, now: date) -> bool:
    pub_date = _iso_to_date(published)
    if pub_date is None:
        # No reliable age — treat as eligible (the S2/OA check is cheap).
        return True
    return (now - pub_date) >= timedelta(days=min_age_days)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _s2_looks_published(row: dict[str, Any]) -> PublicationInfo | None:
    doi = rd.normalize_doi(row.get("doi"))
    if not doi or rd.is_arxiv_doi(doi):
        return None
    venue_type = (row.get("venue_type") or "").lower()
    venue_name = row.get("venue")
    if venue_type in JOURNAL_VENUE_TYPES or (venue_name and "arxiv" not in venue_name.lower()):
        return PublicationInfo(
            found=True,
            journal_doi=doi,
            venue=venue_name,
            publication_date=row.get("publication_date"),
            source="semanticscholar",
        )
    return None


def _oa_looks_published(work: dict[str, Any] | None) -> PublicationInfo | None:
    if not work:
        return None
    doi = rd.normalize_doi(oa.work_doi(work))
    if not doi or rd.is_arxiv_doi(doi):
        return None
    primary = work.get("primary_location") or {}
    source = (primary.get("source") or {}) if isinstance(primary, dict) else {}
    stype = (source.get("type") or "").lower()
    venue = oa.work_venue(work)
    if stype in JOURNAL_VENUE_TYPES or (venue and "arxiv" not in venue.lower()):
        pub = oa.work_publication_date(work)
        return PublicationInfo(
            found=True,
            journal_doi=doi,
            venue=venue,
            publication_date=pub.isoformat() if pub else None,
            source="openalex",
        )
    return None


def detect_publication(
    paper: Paper,
    *,
    min_age_days: int = 180,
    now: date | None = None,
) -> PublicationInfo:
    """Check whether an arXiv paper has been published in a journal.

    Re-fetches the arXiv record to read the authoritative first-version
    ``<published>`` date; papers younger than ``min_age_days`` short-circuit
    without hitting S2/OA.
    """
    now = now or datetime.now(UTC).date()
    arxiv_id = paper.arxiv_id
    if not arxiv_id:
        return PublicationInfo(found=False, reason="not an arXiv paper")

    published: str | None = None
    try:
        entry = arxiv_source.fetch_one(arxiv_id.split("v", 1)[0])
        published = entry.published if entry else None
    except Exception as e:  # noqa: BLE001 - arXiv hiccup shouldn't abort detection
        logger.info("arXiv fetch_one failed for %s: %s", arxiv_id, e)

    if not _is_old_enough(published, min_age_days=min_age_days, now=now):
        return PublicationInfo(found=False, reason="too young")

    # Semantic Scholar is the primary source — it exposes publicationVenue.type.
    try:
        s2_row = s2.fetch_paper(f"ARXIV:{arxiv_id.split('v', 1)[0]}")
    except Exception as e:  # noqa: BLE001
        logger.info("S2 lookup failed for %s: %s", arxiv_id, e)
        s2_row = None
    if s2_row:
        info = _s2_looks_published(s2_row)
        if info:
            return info

    # OpenAlex fallback (primary_location.source.type is the journal signal).
    try:
        work = oa.lookup_by_arxiv_id(arxiv_id.split("v", 1)[0], title_hint=paper.title)
    except Exception as e:  # noqa: BLE001
        logger.info("OpenAlex lookup failed for %s: %s", arxiv_id, e)
        work = None
    info = _oa_looks_published(work)
    if info:
        return info

    return PublicationInfo(found=False, reason="no journal DOI found")


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def select_candidates(
    session: Session,
    *,
    limit: int = 50,
    now: datetime | None = None,
    min_age_days: int = 180,
    throttle_days: int = 30,
) -> list[Paper]:
    """arXiv papers without a journal_doi that are due for a publication check.

    Eligible when: in library, not discarded, has arxiv_id, no journal_doi yet,
    old enough (publication_date or created_at), and not checked within
    ``throttle_days``. Never-checked rows (published_checked_at IS NULL) come
    first.
    """
    now = now or datetime.now(UTC)
    age_cutoff = now - timedelta(days=min_age_days)
    throttle_cutoff = now - timedelta(days=throttle_days)

    stmt = (
        select(Paper)
        .where(
            Paper.in_library.is_(True),
            Paper.discarded.is_(False),
            Paper.arxiv_id.is_not(None),
            Paper.journal_doi.is_(None),
            or_(
                Paper.publication_date.is_(None),
                Paper.publication_date < age_cutoff.date(),
            ),
            Paper.created_at < age_cutoff,
            or_(
                Paper.published_checked_at.is_(None),
                Paper.published_checked_at < throttle_cutoff,
            ),
        )
        .order_by(Paper.published_checked_at.is_(None).desc(), Paper.created_at.asc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


# ---------------------------------------------------------------------------
# Apply the journal version
# ---------------------------------------------------------------------------


def _set_pdf_variant(pdf_files: dict[str, Any] | None, key: str, rel: str) -> dict[str, Any]:
    out = dict(pdf_files or {})
    out[key] = rel
    return out


def check_and_apply(
    session: Session,
    cfg: CarrelYAML,
    paper_id: str,
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> Paper:
    """Detect a published version for one paper and, if found, switch to it.

    Always stamps ``published_checked_at``. When detection finds a journal DOI
    but the remote PDF cannot be fetched, the DOI is still recorded and the
    existing arXiv version is left active (caller may retry later).
    """
    env = EnvSettings()
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise ProcessError(f"paper not found: {paper_id}")

    def _emit(progress: dict) -> None:
        if on_progress is not None:
            on_progress({"paper_id": paper.id, "paper_title": paper.title, **progress})

    if paper.journal_doi and not force:
        _emit({"stage": "publication", "detail": "Already checked"})
        return paper

    _emit({"stage": "publication", "detail": "Checking for journal version…"})
    info = detect_publication(
        paper,
        min_age_days=env.remote_journal_min_age_days,
    )
    paper.published_checked_at = datetime.now(UTC)
    session.add(paper)
    session.commit()

    if not info.found or not info.journal_doi:
        _emit({"stage": "publication", "detail": info.reason or "No journal version found"})
        return paper

    # Record the detection regardless of whether the PDF fetch succeeds below.
    paper.journal_doi = info.journal_doi
    if info.venue and not paper.venue:
        paper.venue = info.venue
    if info.publication_date and not paper.publication_date:
        paper.publication_date = _iso_to_date(info.publication_date)
    session.add(paper)
    session.commit()
    _emit({
        "stage": "publication",
        "detail": f"Found journal version ({info.source}); fetching PDF…",
        "journal_doi": info.journal_doi,
    })

    work_dir, pdf_dest, _md, rel_prefix = paper_paths(paper, cfg)

    # Fetch journal.pdf to a temp name via the institutional host.
    try:
        tmp_path = rd.download_paper(
            info.journal_doi, work_dir, filename=JOURNAL_TMP_FILENAME
        )
    except rd.RemoteError as e:
        msg = f"journal version detected but PDF fetch failed: {e}"
        logger.warning("publication_check %s: %s", paper_id, msg)
        paper.error = msg[:1000]
        session.add(paper)
        session.commit()
        _emit({"stage": "publication", "detail": "PDF fetch failed; kept arXiv version"})
        return paper

    journal_path = work_dir / JOURNAL_FILENAME
    tmp_path.replace(journal_path)

    # Back up the currently-active PDF as arxiv.pdf if we don't have one yet,
    # then promote journal.pdf to paper.pdf. pdf_files records both variants.
    if pdf_dest.exists() and not (work_dir / ARXIV_FILENAME).exists():
        shutil.copyfile(pdf_dest, work_dir / ARXIV_FILENAME)
    shutil.copyfile(journal_path, pdf_dest)

    rel = lambda name: f"{rel_prefix}/{name}"  # noqa: E731
    paper.pdf_files = _set_pdf_variant(
        _set_pdf_variant(paper.pdf_files, "arxiv", rel(ARXIV_FILENAME)),
        "journal",
        rel(JOURNAL_FILENAME),
    )
    paper.pdf_origin = "journal"
    paper.oa_status = "institutional"
    paper.error = None

    # Now that the new active PDF is safely in place, drop the old parsed
    # artifacts so the re-run below regenerates them from the journal PDF.
    md_path = work_dir / "paper.md"
    images_dir = work_dir / "images"
    if md_path.exists():
        md_path.unlink()
    if images_dir.exists():
        shutil.rmtree(images_dir, ignore_errors=True)
    paper.md_path = None
    paper.status = PaperStatus.pending.value
    session.exec(delete(Chunk).where(Chunk.paper_id == paper.id))
    session.add(paper)
    session.commit()

    _emit({"stage": "publication", "detail": "Re-processing journal PDF…"})
    return process_paper(session, cfg, paper.id, on_progress=on_progress)


# ---------------------------------------------------------------------------
# Batch + closed-paper sweep
# ---------------------------------------------------------------------------


def check_pending(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 50,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Check a batch of due arXiv papers for journal versions."""
    env = EnvSettings()
    papers = select_candidates(
        session,
        limit=limit,
        min_age_days=env.remote_journal_min_age_days,
        throttle_days=env.remote_journal_check_throttle_days,
    )
    counts = {"candidates": len(papers), "found": 0, "failed": 0, "skipped": 0}
    total = len(papers)

    for i, paper in enumerate(papers, start=1):
        title = paper.title

        def _cb(progress: dict, idx: int = i) -> None:
            if on_progress is not None:
                on_progress({**progress, "index": idx, "total": total, "title": title})

        try:
            before = paper.journal_doi
            check_and_apply(session, cfg, paper.id, on_progress=_cb)
            session.refresh(paper)
            if paper.journal_doi and not before:
                counts["found"] += 1
        except Exception as e:  # noqa: BLE001 - recorded per paper
            logger.info("publication check failed for %s: %s", paper.id, e)
            counts["failed"] += 1

    logger.info(
        "publication check batch done: candidates=%d found=%d failed=%d",
        counts["candidates"], counts["found"], counts["failed"],
    )
    return counts


def select_remote_candidates(session: Session, *, limit: int = 50) -> list[Paper]:
    """Papers with no cached PDF that the institutional host might be able to get."""
    stmt = (
        select(Paper)
        .where(
            Paper.in_library.is_(True),
            Paper.pdf_path.is_(None),
            Paper.status.in_([PaperStatus.pending.value, PaperStatus.failed.value]),
            or_(
                Paper.pdf_url.is_not(None),
                Paper.arxiv_id.is_not(None),
                Paper.doi.is_not(None),
                Paper.journal_doi.is_not(None),
            ),
        )
        .order_by(Paper.created_at.desc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def fill_closed_papers(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 50,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Try to download PDFs for closed papers (HTTP + institutional fallback).

    The remote fallback lives inside :func:`process_paper`; this just drives the
    batch. No-op (returns zeros) when the institutional host is not configured.
    """
    if not rd.is_configured():
        return {"candidates": 0, "parsed": 0, "failed": 0, "skipped": 0, "reason": "remote not configured"}

    papers = select_remote_candidates(session, limit=limit)
    counts = {"candidates": len(papers), "parsed": 0, "failed": 0, "skipped": 0}
    total = len(papers)

    for i, paper in enumerate(papers, start=1):
        title = paper.title

        def _cb(progress: dict, idx: int = i) -> None:
            if on_progress is not None:
                on_progress({**progress, "index": idx, "total": total, "title": title})

        try:
            process_paper(session, cfg, paper.id, on_progress=_cb)
            counts["parsed"] += 1
        except Exception as e:  # noqa: BLE001 - recorded on paper.error
            logger.info("remote fill failed for %s: %s", paper.id, e)
            counts["failed"] += 1

    logger.info(
        "remote fill done: candidates=%d parsed=%d failed=%d",
        counts["candidates"], counts["parsed"], counts["failed"],
    )
    return counts
