"""Tests for the chunk + embed pipeline (parsed -> ready)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from carrel.config import CarrelYAML
from carrel.models import Chunk, Paper, PaperStatus
from carrel.pipeline import embed as emb_pipe
from sqlmodel import select


def _make_paper(session, **kw) -> Paper:
    base = dict(
        id="W1",
        id_kind="openalex",
        title="Some Paper",
        status=PaperStatus.parsed.value,
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


def _write_md(cfg: CarrelYAML, paper: Paper, body: str) -> str:
    """Write a parsed-md file at the storage-root-relative path on paper.md_path.
    Caller is responsible for committing the paper change.
    """
    cfg.storage.root.mkdir(parents=True, exist_ok=True)
    rel = f"papers/{paper.id}/paper.md"
    full = cfg.storage.root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    paper.md_path = rel
    return rel


def test_embed_paper_happy_path(session, cfg: CarrelYAML, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md")
    _write_md(
        cfg, p,
        "# Intro\n\nThis is a small body that will fit in one chunk.\n"
        + ("extra words " * 200),
    )

    def _fake_embed(texts, **kwargs):
        # Return one vector per input, regardless of count
        return [[0.1] * cfg.embeddings.dim for _ in texts]

    monkeypatch.setattr(emb_pipe.emb, "embed_texts", _fake_embed)

    out = emb_pipe.embed_paper(session, cfg, p.id)
    session.refresh(out)
    assert out.status == PaperStatus.ready.value
    assert out.error is None
    rows = session.exec(select(Chunk).where(Chunk.paper_id == p.id)).all()
    assert len(rows) >= 1
    assert all(len(r.embedding) == cfg.embeddings.dim for r in rows)


def test_embed_paper_no_md_path_marks_failed(session, cfg: CarrelYAML, tmp_path):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path=None)
    with pytest.raises(emb_pipe.EmbedError, match="no md_path"):
        emb_pipe.embed_paper(session, cfg, p.id)


def test_embed_paper_missing_md_file_marks_failed(session, cfg: CarrelYAML, tmp_path):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/missing.md")
    with pytest.raises(emb_pipe.EmbedError, match="missing on disk"):
        emb_pipe.embed_paper(session, cfg, p.id)


def test_embed_paper_embedding_failure_marks_failed(
    session, cfg: CarrelYAML, tmp_path, monkeypatch,
):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md")
    _write_md(cfg, p, "# Body\n\nsome content " * 30)

    def _boom(texts, **kwargs):
        raise RuntimeError("rate limit")

    monkeypatch.setattr(emb_pipe.emb, "embed_texts", _boom)

    with pytest.raises(emb_pipe.EmbedError):
        emb_pipe.embed_paper(session, cfg, p.id)
    session.refresh(p)
    assert p.status == PaperStatus.failed.value
    assert "rate limit" in (p.error or "")


def test_embed_paper_dim_mismatch_raises(session, cfg: CarrelYAML, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md")
    _write_md(cfg, p, "# Body\n\nstuff " * 50)

    def _wrong_dim(texts, **kwargs):
        return [[0.1] * (cfg.embeddings.dim - 1) for _ in texts]

    monkeypatch.setattr(emb_pipe.emb, "embed_texts", _wrong_dim)

    with pytest.raises(emb_pipe.EmbedError, match="dim"):
        emb_pipe.embed_paper(session, cfg, p.id)
    session.refresh(p)
    assert p.status == PaperStatus.failed.value


def test_embed_paper_is_idempotent(session, cfg: CarrelYAML, tmp_path, monkeypatch):
    """Re-running on an already-ready paper should be a no-op."""
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md", status=PaperStatus.ready.value)
    _write_md(cfg, p, "# Body\n\nstuff " * 50)

    # Pre-seed chunks so it short-circuits
    session.add(Chunk(paper_id=p.id, chunk_index=0, content_md="x", embedding=[0.0] * 4))
    session.commit()

    called = {"n": 0}

    def _should_not_run(texts, **kwargs):
        called["n"] += 1
        return [[0.1] * cfg.embeddings.dim for _ in texts]

    monkeypatch.setattr(emb_pipe.emb, "embed_texts", _should_not_run)
    out = emb_pipe.embed_paper(session, cfg, p.id)
    assert out.status == PaperStatus.ready.value
    assert called["n"] == 0  # embedding never called


def test_embed_paper_replaces_stale_chunks(session, cfg: CarrelYAML, tmp_path, monkeypatch):
    """A parsed paper with leftover chunks re-embeds them."""
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md", status=PaperStatus.parsed.value)
    _write_md(cfg, p, "# Body\n\nstuff " * 50)
    session.add(Chunk(paper_id=p.id, chunk_index=0, content_md="stale", embedding=[0.0] * 4))
    session.commit()

    monkeypatch.setattr(
        emb_pipe.emb, "embed_texts",
        lambda texts, **k: [[0.5] * cfg.embeddings.dim for _ in texts],
    )
    out = emb_pipe.embed_paper(session, cfg, p.id)
    session.refresh(out)
    assert out.status == PaperStatus.ready.value
    rows = session.exec(select(Chunk).where(Chunk.paper_id == p.id)).all()
    assert all(r.content_md != "stale" for r in rows)


def test_select_pending_embed_picks_parsed_and_failed(session, cfg, tmp_path):
    cfg.storage.root = tmp_path / "data"
    _make_paper(
        session, id="W-pend", md_path="papers/W-pend/m.md",
        status=PaperStatus.parsed.value,
    )
    _make_paper(
        session, id="W-fail", md_path="papers/W-fail/m.md",
        status=PaperStatus.failed.value,
    )
    _make_paper(
        session, id="W-ready", md_path="papers/W-ready/m.md",
        status=PaperStatus.ready.value,
    )
    _make_paper(
        session, id="W-pending-nomd", md_path=None,
        status=PaperStatus.parsed.value,
    )

    rows = emb_pipe.select_pending_embed(session, limit=10)
    ids = {r.id for r in rows}
    assert ids == {"W-pend", "W-fail"}
