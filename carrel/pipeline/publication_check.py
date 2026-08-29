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
import time
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
    session: Session | None = None,
) -> PublicationInfo:
    """Check whether an arXiv paper has been published in a journal.

    Re-fetches the arXiv record to read the authoritative first-version
    ``<published>`` date; papers younger than ``min_age_days`` short-circuit
    without hitting S2/OA.

    When a SQLAlchemy ``session`` is provided, the OpenAlex lookup is
    routed through the persistent cache (see
    :func:`carrel.cache.openalex_works.lookup_work_by_arxiv_id`) so the
    second paper to be checked on the same arXiv id skips the live call.
    Without a session the legacy direct call is used.
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
        if session is not None:
            from carrel.cache import openalex_works as cache

            work = cache.lookup_work_by_arxiv_id(
                session,
                arxiv_id.split("v", 1)[0],
                title_hint=paper.title,
            )
        else:
            work = oa.lookup_by_arxiv_id(
                arxiv_id.split("v", 1)[0], title_hint=paper.title
            )
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
        session=session,
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


def _has_remote_identifier(paper: Paper) -> bool:
    """True if the paper has a usable identifier for the remote jump host.

    The HTTP path can use ``pdf_url`` directly, but the institutional CLI needs
    a DOI or arXiv id. We mirror :func:`carrel.pipeline.process._remote_identifier`
    here so the batch layer can skip papers the remote can never serve (instead
    of letting them churn through SSH and fail every time).
    """
    if paper.journal_doi and rd.normalize_doi(paper.journal_doi):
        return True
    if paper.doi and rd.normalize_doi(paper.doi):
        return True
    if paper.arxiv_id:
        return True
    return False


def _classify_remote_failure(exc: BaseException) -> str:
    """Bucket a download failure as 'permanent' or 'transient' for the retry pass.

    The remote layer raises ``RemotePermanentError`` for bad identifiers /
    malformed magic and ``RemoteTransientError`` for SSH/network/timeout. When
    the failure is wrapped (e.g. the per-paper ``ProcessError`` raised from
    ``_try_remote_download``), we follow ``__cause__`` to recover the original
    classification.
    """
    if isinstance(exc, rd.RemotePermanentError):
        return "permanent"
    if isinstance(exc, rd.RemoteTransientError):
        return "transient"
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, rd.RemotePermanentError):
        return "permanent"
    if isinstance(cause, rd.RemoteTransientError):
        return "transient"
    # Anything else (parse error, MinerU hiccup, generic ProcessError) — assume
    # transient so the paper gets one more chance at the end of the batch.
    return "transient"


def fill_closed_papers(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 50,
    on_progress: ProgressCallback | None = None,
    retry_pass: bool = True,
    retry_backoff_seconds: float = 3.0,
) -> dict[str, int]:
    """Try to download PDFs for closed papers (HTTP + institutional fallback).

    The remote fallback lives inside :func:`process_paper`; this just drives the
    batch. No-op (returns zeros) when the institutional host is not configured.

    Robustness measures on top of the per-call retries inside
    :mod:`carrel.sources.remote_downloader`:

    * **Skip** — papers with no usable identifier for the remote host (e.g.
      metadata-only rows with no DOI/arxiv_id) are *skipped*, not failed, so
      they don't show up in the ``failed`` counter and don't waste an SSH
      connection.
    * **End-of-batch retry** — after the first sweep, papers that failed with
      a *transient* error (SSH blip, jump host down, timeout, SFTP hiccup)
      get one extra attempt. The whole batch sleeps ``retry_backoff_seconds``
      first so the jump host has a moment to recover. Permanent errors
      (rejected identifier, bad magic) are NOT retried; they would just fail
      the same way and cost another connection.
    * **Structured counts** — the returned dict now distinguishes
      ``retried`` (attempts made in the second pass) and ``retried_ok``
      (second-pass successes), in addition to the original
      ``candidates/parsed/failed/skipped`` keys.
    """
    if not rd.is_configured():
        return {
            "candidates": 0, "parsed": 0, "failed": 0, "skipped": 0,
            "retried": 0, "retried_ok": 0, "reason": "remote not configured",
        }

    papers = select_remote_candidates(session, limit=limit)
    counts: dict[str, int] = {
        "candidates": len(papers), "parsed": 0, "failed": 0, "skipped": 0,
        "retried": 0, "retried_ok": 0,
    }
    total = len(papers)

    # Track the two failure buckets separately so the math is exact at the end
    # and the retry pass only touches the transient ones.
    transient_failures: list[Paper] = []
    permanent_failures: list[Paper] = []

    def _run_one(paper: Paper, idx: int, *, is_retry: bool) -> None:
        """Try one paper. Records skip/failure into the shared buckets.

        A successful call increments ``counts["parsed"]`` (and
        ``counts["retried_ok"]`` if this was a retry attempt).
        """
        title = paper.title

        def _cb(progress: dict) -> None:
            if on_progress is not None:
                on_progress({
                    **progress, "index": idx, "total": total,
                    "title": title, "is_retry": is_retry,
                })

        # Defensive: the SQL filter already excluded these, but a race or a
        # paper edited mid-batch could land here. Don't waste an SSH connection.
        if not _has_remote_identifier(paper):
            logger.info("remote fill skip %s: no remote identifier", paper.id)
            counts["skipped"] += 1
            return

        try:
            process_paper(session, cfg, paper.id, on_progress=_cb)
        except Exception as e:  # noqa: BLE001 - recorded on paper.error above
            kind = _classify_remote_failure(e)
            logger.info(
                "remote fill %s failure for %s (%s): %s",
                kind, paper.id, type(e).__name__, e,
            )
            if kind == "transient":
                transient_failures.append(paper)
            else:
                permanent_failures.append(paper)
            return

        counts["parsed"] += 1
        if is_retry:
            counts["retried_ok"] += 1

    # First pass — every candidate gets a fair shot.
    for i, paper in enumerate(papers, start=1):
        _run_one(paper, i, is_retry=False)

    # End-of-batch retry pass: only the transient failures come back. A blanket
    # jump-host hiccup usually clears in a few seconds, so we wait briefly
    # before retrying. Capped at one round — if the second attempt also fails
    # transiently, the jump host is genuinely down and another round just
    # burns connections.
    if retry_pass and transient_failures:
        to_retry = transient_failures
        transient_failures = []  # failures from the retry pass land in here fresh
        counts["retried"] = len(to_retry)
        if retry_backoff_seconds > 0:
            logger.info(
                "remote fill retry pass: %d papers after %.1fs backoff",
                len(to_retry), retry_backoff_seconds,
            )
            time.sleep(retry_backoff_seconds)
        for j, paper in enumerate(to_retry, start=1):
            # Refresh so we see any state written by the failed first attempt
            # (e.g. a partial download or a ``paper.error`` line).
            session.refresh(paper)
            _run_one(paper, total + j, is_retry=True)

    # The invariant: every paper is in exactly one of {parsed, skipped, failed}
    # where ``parsed`` includes the ``retried_ok`` subset.
    counts["failed"] = len(transient_failures) + len(permanent_failures)

    logger.info(
        "remote fill done: candidates=%d parsed=%d failed=%d skipped=%d "
        "retried=%d retried_ok=%d",
        counts["candidates"], counts["parsed"], counts["failed"],
        counts["skipped"], counts["retried"], counts["retried_ok"],
    )
    return counts
