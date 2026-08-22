"""PDF download + MinerU parse pipeline (M3).

Drives a paper through the state machine::

    pending --(download PDF)--> pdf_ready --(MinerU)--> parsed
                                                  └─(best-effort LLM summary)─> summarized

Download and parse are individually idempotent: if ``paper.pdf`` is already on
disk we skip the download, and if ``paper.md`` exists we skip parsing. The
summary step is chained after a successful parse but is non-fatal: a missing
API key or an LLM error leaves the paper at ``parsed`` (embedding still
accepts it). Failures in download/parse are recorded on ``Paper.error`` and
the status is left as ``failed``; a manual retry simply calls
:func:`process_paper` again.

Like :mod:`carrel.pipeline.runner`, this module is synchronous — a single-user
box processes one paper at a time, and MinerU itself is the bottleneck.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlmodel import Session, or_, select

from carrel.config import CarrelYAML
from carrel.models import Paper, PaperStatus
from carrel.sources import mineru_client
from carrel.sources.pdf_download import download_pdf_with_fallback, safe_paper_dir

logger = logging.getLogger(__name__)

PDF_FILENAME = "paper.pdf"
MD_FILENAME = "paper.md"


class ProcessError(Exception):
    """A paper cannot be processed (e.g. no PDF URL, bad URL, parse failed)."""


# A progress callback receives a dict describing the current stage:
#   {"stage": "download"|"parse"|"done", "detail": str, ...optional fields}
ProgressCallback = Callable[[dict], None]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def paper_paths(paper: Paper, cfg: CarrelYAML) -> tuple[Path, Path, Path, str]:
    """Return (work_dir, pdf_dest, md_dest, rel_prefix) for a paper.

    ``rel_prefix`` is the storage-root-relative directory prefix stored in
    ``Paper.pdf_path`` / ``Paper.md_path`` (e.g. ``papers/W12345``).
    """
    root = Path(cfg.storage.root)
    work_dir = safe_paper_dir(paper.id, root, cfg.storage.papers_subdir)
    rel_prefix = work_dir.relative_to(root).as_posix()
    return work_dir, work_dir / PDF_FILENAME, work_dir / MD_FILENAME, rel_prefix


def _remote_identifier(paper: Paper) -> str | None:
    """Pick the best identifier to send to the institutional jump host.

    Prefer a journal DOI (the published version), then the stored DOI (with a
    doi.org prefix stripped), then the version-stripped arXiv id.
    """
    from carrel.sources import remote_downloader as rd

    if paper.journal_doi:
        return rd.normalize_doi(paper.journal_doi)
    if paper.doi:
        return rd.normalize_doi(paper.doi)
    if paper.arxiv_id:
        return paper.arxiv_id.split("v", 1)[0]
    return None


def _try_remote_download(
    session: Session, work_dir: Path, rel_prefix: str, paper: Paper
) -> bool:
    """Fallback: download via the institutional SSH jump host.

    Returns True on success (paper updated and committed). Raises ProcessError
    when the remote is configured and attempted but fails. Returns False when
    the remote is not configured or the paper has no usable identifier (so the
    caller preserves its original error).
    """
    from carrel.sources import remote_downloader as rd

    if not rd.is_configured():
        return False
    ident = _remote_identifier(paper)
    if not ident:
        return False

    try:
        rd.download_paper(ident, work_dir, filename=PDF_FILENAME)
    except rd.RemotePermanentError as e:
        raise ProcessError(f"institutional download: {e}") from e
    except rd.RemoteError as e:
        raise ProcessError(f"institutional download failed: {e}") from e

    paper.oa_status = "institutional"
    paper.pdf_origin = "institutional"
    paper.pdf_path = f"{rel_prefix}/{PDF_FILENAME}"
    paper.status = PaperStatus.pdf_ready.value
    session.add(paper)
    session.commit()
    logger.info(
        "institutional download for %s via %s -> %s", paper.id, ident, paper.pdf_path
    )
    return True


def _pdf_candidates(paper: Paper) -> list[str]:
    """Build an ordered list of PDF URLs to try for this paper.

    The stored ``paper.pdf_url`` goes first; then any additional candidates
    extracted from the OpenAlex work cached in ``raw_meta`` (repository/arXiv
    copies that OpenAlex lists alongside a publisher URL); finally an arXiv PDF
    as a last resort when the paper has an ``arxiv_id``. OpenAlex sometimes
    mislabels a publisher HTML page as ``pdf_url``, so the downloader works
    through this list and keeps the first one that is genuinely a PDF.
    """
    urls: list[str] = []
    seen: set[str] = set()

    def _add(u: str | None) -> None:
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    _add(paper.pdf_url)

    meta = paper.raw_meta or {}
    work = meta.get("openalex") if isinstance(meta, dict) else None
    if work is None and isinstance(meta, dict) and "open_access" in meta:
        work = meta  # pure-OpenAlex record stores the work directly

    if isinstance(work, dict):
        from carrel.sources import openalex_client as oa
        for u in oa.work_pdf_candidates(work):
            _add(u)

    if paper.arxiv_id:
        _add(f"https://arxiv.org/pdf/{paper.arxiv_id}.pdf")

    return urls


# ---------------------------------------------------------------------------
# Per-paper processing
# ---------------------------------------------------------------------------


def process_paper(
    session: Session,
    cfg: CarrelYAML,
    paper_id: str,
    *,
    client: httpx.Client | None = None,
    on_progress: ProgressCallback | None = None,
) -> Paper:
    """Download and parse one paper, advancing its status. Returns the paper."""
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise ProcessError(f"paper not found: {paper_id}")

    # Clear a previous error so the UI reflects the fresh attempt.
    paper.error = None

    def _emit(progress: dict) -> None:
        if on_progress is not None:
            on_progress(
                {"paper_id": paper.id, "paper_title": paper.title, **progress}
            )

    try:
        _emit({"stage": "download", "detail": "Downloading PDF…"})
        _step_download(session, cfg, paper, client=client)
        _emit({"stage": "parse", "detail": "Queued for parsing…"})
        _step_parse(session, cfg, paper, client=client, emit=_emit)
        # M4: best-effort LLM summary. Runs chained after a successful parse;
        # failures are non-fatal — the paper stays `parsed` and embedding can
        # still run. We import lazily and guard against a missing API key so
        # boxes without an LLM configured parse quietly.
        try:
            from carrel.pipeline.summarize import SummarizeError, summarize_paper

            _emit({"stage": "summarize", "detail": "Generating summary…"})
            summarize_paper(
                session, cfg, paper.id,
                on_progress=lambda d: _emit({"stage": "summarize", **d}),
            )
        except SummarizeError as e:
            logger.info("summarize skipped/failed for %s: %s", paper_id, e)
        except Exception as e:  # noqa: BLE001 - never poison a successful parse
            logger.warning("summarize crashed for %s: %s", paper_id, e)
        # Best-effort topic classification. Metadata-only (no PDF needed), so it
        # runs after summarize; failures are non-fatal and never regress status.
        try:
            from carrel.pipeline.topics import TopicsError, topics_paper

            _emit({"stage": "topics", "detail": "Classifying topics…"})
            topics_paper(
                session, cfg, paper.id,
                on_progress=lambda d: _emit({"stage": "topics", **d}),
            )
        except TopicsError as e:
            logger.info("topics skipped/failed for %s: %s", paper_id, e)
        except Exception as e:  # noqa: BLE001 - never poison a successful parse
            logger.warning("topics crashed for %s: %s", paper_id, e)
    except Exception as e:
        paper.status = PaperStatus.failed.value
        paper.error = f"{type(e).__name__}: {e}"[:1000]
        paper.updated_at = datetime.now(UTC)
        session.add(paper)
        session.commit()
        logger.warning("process %s failed: %s", paper_id, e)
        raise

    paper.updated_at = datetime.now(UTC)
    session.add(paper)
    session.commit()
    session.refresh(paper)
    return paper


def _step_download(
    session: Session, cfg: CarrelYAML, paper: Paper, *, client: httpx.Client | None
) -> None:
    if paper.status == PaperStatus.parsed.value and paper.md_path:
        return  # already fully processed

    work_dir, pdf_dest, _md, rel_prefix = paper_paths(paper, cfg)

    # Reuse an existing PDF on disk (e.g. previous run parsed but failed later).
    if pdf_dest.exists() and pdf_dest.stat().st_size > 0:
        if not paper.pdf_path:
            paper.pdf_path = f"{rel_prefix}/{PDF_FILENAME}"
        if paper.status not in (PaperStatus.pdf_ready.value, PaperStatus.parsed.value):
            paper.status = PaperStatus.pdf_ready.value
        session.add(paper)
        logger.info("reuse existing PDF for %s", paper.id)
        return

    # A stored pdf_url is not required: _pdf_candidates() synthesizes an arXiv
    # PDF from arxiv_id and pulls repository copies from raw_meta. When no
    # candidate exists (truly closed access, metadata only) or every HTTP
    # candidate fails, fall back to the institutional SSH jump host (if
    # configured) before giving up.
    candidates = _pdf_candidates(paper)
    dl = cfg.download
    http_error: Exception | None = None
    used_url: str | None = None
    if candidates:
        try:
            _path, used_url = download_pdf_with_fallback(
                candidates,
                work_dir,
                filename=PDF_FILENAME,
                timeout=dl.request_timeout_seconds,
                max_bytes=dl.max_bytes,
                user_agent=dl.user_agent,
                client=client,
            )
        except Exception as e:  # noqa: BLE001 - converted to remote fallback below
            http_error = e
            used_url = None
    else:
        http_error = ProcessError(
            "no PDF URL available (closed access or metadata only)"
        )

    if used_url is not None:
        # If a fallback candidate (e.g. an arXiv copy) succeeded where the stored
        # publisher URL served HTML, remember it so future retries don't repeat
        # the bad URL. We now have a verified PDF, so mark the paper OA regardless
        # of what the source metadata claimed.
        if used_url != paper.pdf_url:
            logger.info("PDF for %s resolved via fallback %s", paper.id, used_url)
            paper.pdf_url = used_url
        paper.oa_status = "oa"
        paper.pdf_origin = (
            "arxiv" if used_url.startswith("https://arxiv.org/pdf/") else "oa"
        )
        paper.pdf_path = f"{rel_prefix}/{PDF_FILENAME}"
        paper.status = PaperStatus.pdf_ready.value
        session.add(paper)
        session.commit()
        logger.info("downloaded PDF for %s -> %s", paper.id, paper.pdf_path)
        return

    # HTTP path failed or had no candidates. Try the institutional jump host.
    if _try_remote_download(session, work_dir, rel_prefix, paper):
        return
    if isinstance(http_error, ProcessError):
        raise http_error
    raise ProcessError(
        f"no PDF available (HTTP download failed: {http_error})"
    ) from http_error


def _step_parse(
    session: Session,
    cfg: CarrelYAML,
    paper: Paper,
    *,
    client: httpx.Client | None,
    emit: ProgressCallback | None = None,
) -> None:
    work_dir, pdf_dest, md_dest, rel_prefix = paper_paths(paper, cfg)

    if md_dest.exists() and md_dest.stat().st_size > 0:
        if not paper.md_path:
            paper.md_path = f"{rel_prefix}/{MD_FILENAME}"
        paper.status = PaperStatus.parsed.value
        session.add(paper)
        if emit:
            emit({"stage": "parse", "detail": "Already parsed"})
        return

    if not pdf_dest.exists():
        raise ProcessError(f"PDF missing on disk: {pdf_dest}")

    # Map MinerU's low-level task events into user-facing stage text.
    def _on_mineru(event: str, info: dict) -> None:
        if emit is None:
            return
        if event == "status":
            st = info.get("status")
            ahead = info.get("queued_ahead")
            if st == "pending":
                detail = (
                    f"Queued for parsing… (ahead: {ahead})"
                    if ahead is not None
                    else "Queued for parsing…"
                )
                emit({"stage": "parse", "detail": detail, "mineru_status": st})
            elif st == "processing":
                emit({
                    "stage": "parse",
                    "detail": "MinerU is parsing… (1–3 min on CPU)",
                    "mineru_status": st,
                })
            else:
                emit({"stage": "parse", "detail": f"Parsing: {st}", "mineru_status": st})
        elif event == "fetching":
            emit({"stage": "parse", "detail": "Fetching result…"})
        elif event == "submitted":
            emit({"stage": "parse", "detail": "Submitted to MinerU…"})

    mu = cfg.mineru
    result = mineru_client.parse_pdf(
        pdf_dest,
        work_dir,
        base_url=mu.base_url,
        timeout=mu.request_timeout_seconds,
        backend=mu.backend,
        parse_method=mu.parse_method,
        lang_list=mu.lang_list,
        formula_enable=mu.formula_enable,
        table_enable=mu.table_enable,
        client=client,
        on_progress=_on_mineru,
    )
    # parse_pdf writes to work_dir/paper.md; record the relative path.
    paper.md_path = f"{rel_prefix}/{MD_FILENAME}"
    paper.status = PaperStatus.parsed.value
    session.add(paper)
    session.commit()
    logger.info("parsed %s -> %s (%d images)", paper.id, result.md_path, len(result.images))


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------


def select_pending(session: Session, limit: int = 10) -> list[Paper]:
    """Return papers that can be downloaded and are not yet parsed.

    We pick ``pending`` plus previously ``failed`` papers (so a manual "process
    pending" retry picks them up), newest first. A paper is eligible when it has
    either a stored ``pdf_url`` *or* an ``arxiv_id`` — the latter yields an arXiv
    PDF via ``_pdf_candidates`` even when the source record advertised no OA PDF.
    When the institutional SSH jump host is configured, papers that only carry a
    ``doi`` (or ``journal_doi``) are also eligible: ``_step_download`` will fall
    back to the remote server for those closed-access records. Papers with no
    identifier at all are still excluded.
    """
    from carrel.sources import remote_downloader as rd

    has_identifier = or_(
        Paper.pdf_url.is_not(None),
        Paper.arxiv_id.is_not(None),
    )
    if rd.is_configured():
        has_identifier = or_(
            has_identifier,
            Paper.doi.is_not(None),
            Paper.journal_doi.is_not(None),
        )
    stmt = (
        select(Paper)
        .where(
            Paper.in_library.is_(True),
            has_identifier,
            Paper.status.in_(
                [PaperStatus.pending.value, PaperStatus.failed.value]
            ),
        )
        .order_by(Paper.created_at.desc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def process_pending(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 10,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Process up to ``limit`` pending/failed papers. Returns counters.

    ``on_progress`` receives per-paper stage dicts plus ``index``/``total`` and
    the current paper ``title`` so a caller (e.g. the API) can report batch
    progress.
    """
    papers = select_pending(session, limit=limit)
    counts = {"candidates": len(papers), "parsed": 0, "failed": 0, "skipped": 0}
    total = len(papers)

    def _wrap(i: int, title: str):
        def _cb(progress: dict) -> None:
            if on_progress is not None:
                on_progress({**progress, "index": i, "total": total, "title": title})

        return _cb

    # One shared httpx client for downloads (configured UA/timeout); MinerU
    # calls are long-running and use their own client inside the module.
    dl = cfg.download
    with httpx.Client(
        timeout=dl.request_timeout_seconds,
        headers={"User-Agent": dl.user_agent},
        follow_redirects=True,
    ) as client:
        for i, paper in enumerate(papers, start=1):
            try:
                process_paper(
                    session, cfg, paper.id, client=client, on_progress=_wrap(i, paper.title)
                )
                counts["parsed"] += 1
            except Exception as e:  # noqa: BLE001 - recorded on paper.error above
                logger.info("paper %s failed: %s", paper.id, e)
                counts["failed"] += 1

    logger.info(
        "process batch done: candidates=%d parsed=%d failed=%d",
        counts["candidates"], counts["parsed"], counts["failed"],
    )
    return counts
