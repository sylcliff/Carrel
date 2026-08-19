"""Tests for the OA PDF downloader (content validation + atomic write)."""
from __future__ import annotations

import httpx
import pytest
from carrel.sources.pdf_download import (
    DownloadError,
    download_pdf,
    download_pdf_with_fallback,
    looks_like_pdf,
    safe_paper_dir,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_looks_like_pdf_magic():
    import io

    assert looks_like_pdf(io.BytesIO(b"%PDF-1.7\nstuff"))
    assert not looks_like_pdf(io.BytesIO(b"<!doctype html>"))


def test_safe_paper_dir_slugs_ids(tmp_path):
    d = safe_paper_dir("arxiv:2401.12345", tmp_path)
    assert d == tmp_path / "papers" / "arxiv_2401.12345"
    assert d.exists()
    # OpenAlex ids stay filesystem-safe as-is
    d2 = safe_paper_dir("W2741809807", tmp_path)
    assert d2.name == "W2741809807"


def test_download_happy_path(tmp_path):
    pdf_bytes = b"%PDF-1.7\n%fake pdf body\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=pdf_bytes
        )

    out = download_pdf(
        "https://example.org/p.pdf", tmp_path, client=_client(handler), max_bytes=10_000
    )
    assert out.read_bytes() == pdf_bytes
    # No leftover temp file
    assert not (tmp_path / "paper.pdf.part").exists()


def test_download_rejects_html_content_type(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=b"<!doctype html><html>Zenodo landing page</html>",
        )

    with pytest.raises(DownloadError, match="HTML content-type"):
        download_pdf("https://example.org/landing", tmp_path, client=_client(handler))
    assert not (tmp_path / "paper.pdf").exists()


def test_download_rejects_non_pdf_magic_even_with_octet_stream(tmp_path):
    # Some servers send octet-stream for HTML landing pages; magic check catches it.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"},
            content=b"<html>not a pdf at all</html>",
        )

    with pytest.raises(DownloadError, match="bad magic"):
        download_pdf("https://example.org/fake", tmp_path, client=_client(handler))


def test_download_rejects_oversize(tmp_path):
    body = b"%PDF-" + b"x" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(DownloadError, match="max_bytes"):
        download_pdf(
            "https://example.org/big.pdf", tmp_path,
            client=_client(handler), max_bytes=100,
        )


def test_download_rejects_non_200(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"nope")

    with pytest.raises(DownloadError, match="HTTP 404"):
        download_pdf("https://example.org/missing.pdf", tmp_path, client=_client(handler))


def test_download_falls_through_to_valid_pdf(tmp_path):
    # First candidate is a publisher URL that serves an HTML landing page;
    # the second is a real arXiv PDF. The downloader must keep going and
    # report which URL actually worked.
    pdf_bytes = b"%PDF-1.7\nreal pdf\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if "iopscience" in request.url.host:
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                content=b"<!doctype html>paywall page</html>",
            )
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=pdf_bytes
        )

    path, used = download_pdf_with_fallback(
        [
            "https://iopscience.iop.org/article/x/pdf",
            "https://arxiv.org/pdf/2402.09251",
        ],
        tmp_path,
        client=_client(handler),
    )
    assert path.read_bytes() == pdf_bytes
    assert used == "https://arxiv.org/pdf/2402.09251"


def test_download_fallback_raises_when_all_fail(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>nope</html>"
        )

    with pytest.raises(DownloadError, match="no valid PDF among candidates"):
        download_pdf_with_fallback(
            ["https://example.org/a", "https://example.org/b"],
            tmp_path,
            client=_client(handler),
        )
    assert not (tmp_path / "paper.pdf").exists()
