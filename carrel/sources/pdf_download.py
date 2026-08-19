"""OA PDF downloader.

Downloads a paper's PDF from its (possibly untrusted) `pdf_url` to
``<storage>/papers/<safe-id>/paper.pdf``.

OpenAlex's `best_oa_location.pdf_url` is not always a real PDF: some records
point at Zenodo/HTML landing pages or publisher pages that return 200 OK with
text/html. We therefore validate by content-type *and* the ``%PDF`` magic bytes
before committing the file to disk. An atomic temp-file + rename means a failed
download never leaves a half-written ``paper.pdf`` behind.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import BinaryIO

import httpx

logger = logging.getLogger(__name__)

# A PDF starts with "%PDF-" (e.g. b"%PDF-1.7"). We only need the first 5 bytes.
PDF_MAGIC = b"%PDF-"
PDF_CHUNK = 64 * 1024


class DownloadError(Exception):
    """Raised when a PDF cannot be downloaded or fails validation."""


def safe_paper_dir(paper_id: str, storage_root: Path, papers_subdir: str = "papers") -> Path:
    """Return (and create) ``<storage>/papers/<safe-slug>/`` for a paper id.

    Paper ids are either OpenAlex work ids (``W12345``) or ``arxiv:<id>``. The
    ``:`` and ``/`` that can appear in an arXiv id are replaced so the result is
    safe as a single directory name on all platforms.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", paper_id).strip("._") or "unknown"
    d = storage_root / papers_subdir / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def looks_like_pdf(stream: BinaryIO) -> bool:
    """True if the stream's first bytes are the PDF magic. Resets position."""
    head = stream.read(len(PDF_MAGIC))
    stream.seek(0)
    return head.startswith(PDF_MAGIC)


def download_pdf(
    url: str,
    dest_dir: Path,
    *,
    filename: str = "paper.pdf",
    timeout: float = 60.0,
    max_bytes: int = 80 * 1024 * 1024,
    user_agent: str = "Carrel/0.1",
    follow_redirects: bool = True,
    client: httpx.Client | None = None,
) -> Path:
    """Download ``url`` to ``dest_dir/filename``; validate it is really a PDF.

    Returns the final Path. Raises :class:`DownloadError` on network failure,
    non-2xx status, oversize response, HTML content-type, or missing PDF magic.
    """
    path, _ = download_pdf_with_fallback(
        [url],
        dest_dir,
        filename=filename,
        timeout=timeout,
        max_bytes=max_bytes,
        user_agent=user_agent,
        follow_redirects=follow_redirects,
        client=client,
    )
    return path


def download_pdf_with_fallback(
    urls: list[str],
    dest_dir: Path,
    *,
    filename: str = "paper.pdf",
    timeout: float = 60.0,
    max_bytes: int = 80 * 1024 * 1024,
    user_agent: str = "Carrel/0.1",
    follow_redirects: bool = True,
    client: httpx.Client | None = None,
) -> tuple[Path, str]:
    """Try ``urls`` in order, returning ``(path, url_used)`` for the first real PDF.

    OpenAlex sometimes advertises a publisher HTML page as ``pdf_url`` while a
    genuine repository/arXiv PDF sits in another location. Each URL is validated
    by content-type and ``%PDF`` magic; failures fall through to the next
    candidate. Raises :class:`DownloadError` only when every URL fails (its
    message lists each attempt).
    """
    dest = dest_dir / filename
    tmp = dest.with_suffix(dest.suffix + ".part")

    own_client = client is None
    httpx_client = client or httpx.Client(
        timeout=timeout,
        follow_redirects=follow_redirects,
        headers={"User-Agent": user_agent, "Accept": "application/pdf,*/*;q=0.8"},
    )
    attempts: list[str] = []
    try:
        for url in urls:
            if not url:
                continue
            try:
                with httpx_client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        raise DownloadError(f"HTTP {resp.status_code}")

                    ctype = (resp.headers.get("content-type") or "").lower()
                    # Reject obviously-wrong types, but don't trust the server:
                    # some hosts send application/octet-stream for real PDFs, so
                    # we still verify magic bytes below.
                    if "text/html" in ctype:
                        raise DownloadError(f"refusing HTML content-type ({ctype})")

                    total = 0
                    with tmp.open("wb") as f:
                        for chunk in resp.iter_bytes(PDF_CHUNK):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                raise DownloadError(
                                    f"PDF exceeds max_bytes={max_bytes}"
                                )
                            f.write(chunk)
            except httpx.HTTPError as e:
                attempts.append(f"{url}: network error: {e}")
                continue
            except DownloadError as e:
                attempts.append(f"{url}: {e}")
                continue

            # Validate magic bytes before promoting the temp file.
            with tmp.open("rb") as f:
                if not looks_like_pdf(f):
                    attempts.append(f"{url}: not a PDF (bad magic)")
                    continue

            tmp.replace(dest)
            logger.info("downloaded PDF %s -> %s (%d bytes)", url, dest, total)
            return dest, url

        raise DownloadError(
            "no valid PDF among candidates: " + " | ".join(attempts)
        )
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        if own_client:
            httpx_client.close()
