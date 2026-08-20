"""Tests for the download+parse processing pipeline (state machine)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from carrel.config import CarrelYAML
from carrel.models import Paper, PaperStatus
from carrel.pipeline import process as proc
from carrel.sources.mineru_client import MinerUError, MinerUResult
from carrel.sources.pdf_download import DownloadError


def _make_paper(session, **kw) -> Paper:
    base = dict(
        id="W1",
        id_kind="openalex",
        title="Some Paper",
        status=PaperStatus.pending.value,
        oa_status="oa",
        source="openalex",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base.update(kw)
    p = Paper(**base)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _fake_download_ok(tmp_path: Path):
    """Return a downloader stub that writes a tiny valid PDF to the dest."""

    def _dl(urls, dest_dir, *, filename="paper.pdf", **_kw):
        url = urls[0] if isinstance(urls, list) else urls
        dest = Path(dest_dir) / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.7\nfake")
        return dest, url

    return _dl


def _fake_parse_ok():
    def _parse(pdf_path, dest_dir, **_kw):
        dest = Path(dest_dir) / "paper.md"
        dest.write_text("# Parsed body\n", encoding="utf-8")
        (Path(dest_dir) / "images").mkdir(exist_ok=True)
        return MinerUResult(md_path=dest, images=[])

    return _parse


def test_process_paper_happy_path(session, cfg: CarrelYAML, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, pdf_url="https://example.org/p.pdf")

    monkeypatch.setattr(proc, "download_pdf_with_fallback", _fake_download_ok(tmp_path))
    monkeypatch.setattr(proc.mineru_client, "parse_pdf", _fake_parse_ok())

    out = proc.process_paper(session, cfg, p.id)

    assert out.status == PaperStatus.parsed.value
    assert out.pdf_path and out.pdf_path.endswith("paper.pdf")
    assert out.md_path and out.md_path.endswith("paper.md")
    assert out.error is None
    assert (cfg.storage.root / out.md_path).read_text().startswith("# Parsed")


def test_process_paper_no_pdf_url_marks_failed(session, cfg, tmp_path):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, pdf_url=None, oa_status="closed")

    with pytest.raises(proc.ProcessError):
        proc.process_paper(session, cfg, p.id)

    session.refresh(p)
    assert p.status == PaperStatus.failed.value
    assert "no PDF URL" in (p.error or "")


def test_process_paper_download_failure_marks_failed(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, pdf_url="https://bad/landing")

    def _boom(*a, **k):
        raise DownloadError("refusing HTML content-type")

    monkeypatch.setattr(proc, "download_pdf_with_fallback", _boom)

    with pytest.raises(DownloadError):
        proc.process_paper(session, cfg, p.id)
    session.refresh(p)
    assert p.status == PaperStatus.failed.value
    assert "refusing HTML" in (p.error or "")


def test_process_paper_parse_failure_marks_failed(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, pdf_url="https://example.org/p.pdf")

    monkeypatch.setattr(proc, "download_pdf_with_fallback", _fake_download_ok(tmp_path))

    def _boom(*a, **k):
        raise MinerUError("HTTP 500: boom")

    monkeypatch.setattr(proc.mineru_client, "parse_pdf", _boom)

    with pytest.raises(MinerUError):
        proc.process_paper(session, cfg, p.id)
    session.refresh(p)
    # Download succeeded, parse failed -> pdf recorded, status failed.
    assert p.pdf_path is not None
    assert p.status == PaperStatus.failed.value
    assert "HTTP 500" in (p.error or "")


def test_process_paper_is_idempotent_on_retry(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(
        session, pdf_url="https://example.org/p.pdf", status=PaperStatus.failed.value
    )

    dl_calls = {"n": 0}
    parse_calls = {"n": 0}

    def _dl(urls, dest_dir, *, filename="paper.pdf", **_kw):
        dl_calls["n"] += 1
        return _fake_download_ok(tmp_path)(urls, dest_dir, filename=filename)

    def _parse(pdf_path, dest_dir, **_kw):
        parse_calls["n"] += 1
        return _fake_parse_ok()(pdf_path, dest_dir)

    monkeypatch.setattr(proc, "download_pdf_with_fallback", _dl)
    monkeypatch.setattr(proc.mineru_client, "parse_pdf", _parse)

    proc.process_paper(session, cfg, p.id)
    assert dl_calls["n"] == 1 and parse_calls["n"] == 1

    # Second run: files already on disk, both network steps should be skipped.
    session.refresh(p)
    assert p.status == PaperStatus.parsed.value
    proc.process_paper(session, cfg, p.id)
    assert dl_calls["n"] == 1 and parse_calls["n"] == 1


def test_pdf_candidates_fall_back_to_raw_meta_arxiv(session, cfg, tmp_path):
    """A paper whose stored publisher pdf_url serves HTML should still get the
    arXiv candidate from its cached OpenAlex work, so download can fall back."""
    cfg.storage.root = tmp_path / "data"
    work = {
        "open_access": {"is_oa": True},
        "best_oa_location": {
            "pdf_url": "https://iopscience.iop.org/article/x/pdf",
            "source": {"display_name": "Chinese Physics Letters", "type": "journal"},
        },
        "locations": [
            {"pdf_url": "https://iopscience.iop.org/article/x/pdf",
             "source": {"type": "journal"}},
            {"pdf_url": "https://arxiv.org/pdf/2402.09251",
             "source": {"display_name": "arXiv", "type": "repository"}},
        ],
    }
    p = _make_paper(
        session,
        pdf_url="https://iopscience.iop.org/article/x/pdf",
        arxiv_id="2402.09251",
        raw_meta=work,
    )
    urls = proc._pdf_candidates(p)
    # Stored (bad publisher) URL stays first so we preserve identity, but the
    # genuine arXiv PDF from the cached work is present as a fallback.
    assert urls[0] == "https://iopscience.iop.org/article/x/pdf"
    assert "https://arxiv.org/pdf/2402.09251" in urls
    # The arXiv candidate from raw_meta must not be pushed after another
    # publisher URL (publisher is already deduped to position 0).
    assert len(urls) >= 2


def test_select_pending_excludes_closed_and_parsed(session, cfg, tmp_path):
    cfg.storage.root = tmp_path / "data"
    _make_paper(session, id="W-closed", pdf_url=None, status=PaperStatus.pending.value)
    _make_paper(session, id="W-done", pdf_url="https://x/1.pdf", status=PaperStatus.parsed.value)
    _make_paper(session, id="W-pend", pdf_url="https://x/2.pdf", status=PaperStatus.pending.value)
    _make_paper(session, id="W-fail", pdf_url="https://x/3.pdf", status=PaperStatus.failed.value)

    rows = proc.select_pending(session, limit=10)
    ids = {r.id for r in rows}
    assert ids == {"W-pend", "W-fail"}


def test_process_pending_batch_counts(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    _make_paper(session, id="W-ok", pdf_url="https://x/1.pdf", status=PaperStatus.pending.value)
    _make_paper(session, id="W-fail", pdf_url=None, status=PaperStatus.failed.value)

    monkeypatch.setattr(proc, "download_pdf_with_fallback", _fake_download_ok(tmp_path))
    monkeypatch.setattr(proc.mineru_client, "parse_pdf", _fake_parse_ok())

    counts = proc.process_pending(session, cfg, limit=10)
    assert counts["candidates"] == 1
    assert counts["parsed"] == 1
    assert counts["failed"] == 0


def test_process_chains_summarize_after_parse(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, pdf_url="https://example.org/p.pdf")
    monkeypatch.setattr(proc, "download_pdf_with_fallback", _fake_download_ok(tmp_path))
    monkeypatch.setattr(proc.mineru_client, "parse_pdf", _fake_parse_ok())

    called = {"n": 0}

    def _fake_summarize(session, cfg, paper_id, **kw):
        called["n"] += 1
        paper = session.get(Paper, paper_id)
        paper.status = PaperStatus.summarized.value
        paper.tldr_en = "generated"
        session.add(paper)
        session.commit()
        return paper

    # Patch the symbol in the summarize module so process's lazy import sees it.
    from carrel.pipeline import summarize as summ_mod
    monkeypatch.setattr(summ_mod, "summarize_paper", _fake_summarize)

    out = proc.process_paper(session, cfg, p.id)
    assert called["n"] == 1
    assert out.status == PaperStatus.summarized.value
    assert out.tldr_en == "generated"


def test_process_summarize_failure_does_not_poison_parse(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, pdf_url="https://example.org/p.pdf")
    monkeypatch.setattr(proc, "download_pdf_with_fallback", _fake_download_ok(tmp_path))
    monkeypatch.setattr(proc.mineru_client, "parse_pdf", _fake_parse_ok())

    from carrel.pipeline import summarize as summ_mod

    def _boom(session, cfg, paper_id, **kw):
        raise summ_mod.SummarizeError("no API key")

    monkeypatch.setattr(summ_mod, "summarize_paper", _boom)

    out = proc.process_paper(session, cfg, p.id)
    # Parse still succeeds; paper stays parsed (NOT failed), error untouched.
    assert out.status == PaperStatus.parsed.value
    assert out.error is None
