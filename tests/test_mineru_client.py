"""Tests for the MinerU HTTP client (ZIP result extraction + errors)."""
from __future__ import annotations

import io
import zipfile

import httpx
import pytest
from carrel.sources import mineru_client
from carrel.sources.mineru_client import MinerUError, _extract_result_zip, parse_pdf


def _make_result_zip(
    md: bytes = b"# Title\n\nHello",
    images: dict[str, bytes] | None = None,
) -> bytes:
    """Build a MinerU-style result ZIP: <name>/<parse_dir>/<name>.md + images/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("paper/auto/paper.md", md)
        for fname, data in (images or {}).items():
            zf.writestr(f"paper/auto/images/{fname}", data)
        # A non-md file we should ignore
        zf.writestr("paper/auto/paper_middle.json", b"{}")
    return buf.getvalue()


def test_extract_md_and_images(tmp_path):
    zbytes = _make_result_zip(
        md=b"# Hi\n\n![fig](images/fig1.png)",
        images={"fig1.png": b"\x89PNG-fake"},
    )
    res = _extract_result_zip(zbytes, tmp_path)
    assert res.md_path == tmp_path / "paper.md"
    assert "# Hi" in res.markdown
    assert (tmp_path / "images" / "fig1.png").read_bytes() == b"\x89PNG-fake"
    assert len(res.images) == 1


def test_extract_missing_md_raises(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("paper/auto/images/x.png", b"x")
    with pytest.raises(MinerUError, match="no markdown"):
        _extract_result_zip(buf.getvalue(), tmp_path)


def test_extract_skips_path_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.sh", b"bad")
        zf.writestr("paper/auto/paper.md", b"# ok")
    res = _extract_result_zip(buf.getvalue(), tmp_path)
    assert res.markdown == "# ok"
    assert not (tmp_path.parent.parent / "evil.sh").exists()


def test_parse_pdf_async_flow_and_extracts(tmp_path):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfake")
    dest = tmp_path / "out"
    zbytes = _make_result_zip(md=b"# Parsed")

    captured: dict[str, object] = {}
    events: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/tasks" and request.method == "POST":
            captured["ct"] = request.headers.get("content-type", "")
            captured["has_zip_flag"] = b"response_format_zip" in request.content
            return httpx.Response(202, json={"task_id": "t-123"})
        if path == "/tasks/t-123" and request.method == "GET":
            return httpx.Response(
                200, json={"status": "completed", "queued_ahead": 0}
            )
        if path == "/tasks/t-123/result" and request.method == "GET":
            return httpx.Response(
                200, headers={"content-type": "application/zip"}, content=zbytes
            )
        return httpx.Response(404, text="not found")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = parse_pdf(
        pdf,
        dest,
        base_url="http://mineru.test",
        client=client,
        poll_interval=0,
        on_progress=lambda e, i: events.append((e, i)),
    )
    assert "multipart/form-data" in str(captured["ct"])
    assert captured["has_zip_flag"]
    assert res.markdown == "# Parsed"
    assert (dest / "paper.md").exists()
    assert [e[0] for e in events] == ["submitted", "status", "fetching"]
    assert events[0][1]["task_id"] == "t-123"


def test_parse_pdf_raises_on_submit_http_error(tmp_path):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfake")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(MinerUError, match="HTTP 500"):
        parse_pdf(pdf, tmp_path / "o", client=client, poll_interval=0)


def test_parse_pdf_raises_when_task_fails(tmp_path):
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.7\nfake")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/tasks":
            return httpx.Response(202, json={"task_id": "t-9"})
        if path == "/tasks/t-9":
            return httpx.Response(
                200, json={"status": "failed", "error": "model crashed"}
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(MinerUError, match="model crashed"):
        parse_pdf(pdf, tmp_path / "o", client=client, poll_interval=0)


def test_is_healthy():
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "healthy"})

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "unhealthy"})

    c1 = httpx.Client(transport=httpx.MockTransport(ok))
    c2 = httpx.Client(transport=httpx.MockTransport(down))
    assert mineru_client.is_healthy("http://x", client=c1) is True  # type: ignore[arg-type]
    assert mineru_client.is_healthy("http://x", client=c2) is False  # type: ignore[arg-type]
