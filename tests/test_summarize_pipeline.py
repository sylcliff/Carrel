"""Tests for the LLM summarization pipeline (parsed -> summarized)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from carrel.config import CarrelYAML
from carrel.models import Paper, PaperStatus
from carrel.pipeline import summarize as summ_pipe
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
    cfg.storage.root.mkdir(parents=True, exist_ok=True)
    rel = f"papers/{paper.id}/paper.md"
    full = cfg.storage.root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")
    paper.md_path = rel
    return rel


def _fake_llm(**overrides):
    payload = {
        "tldr_en": "We propose a new method for X.",
        "tldr_zh": "我们提出了一种X的新方法。",
        "summary_zh": "本文提出了一种方法。实验表明它有效。结论是该方法优于基线。",
        "keywords": ["method", "benchmark", "neural network", "evaluation"],
    }
    payload.update(overrides)

    def _chat(messages, **kwargs):
        return dict(payload)

    return _chat


@pytest.fixture(autouse=True)
def _has_key(monkeypatch):
    # Pretend an API key is configured so the no-key guard doesn't trip.
    monkeypatch.setattr(summ_pipe.llm, "has_key_for", lambda model: True)


@pytest.fixture(autouse=True)
def _wire_app_engine(session, monkeypatch):
    """Point ``carrel.db.app_engine`` at the test session's engine.

    ``prompts_runtime.get_system`` / ``get_user_template`` open their own
    :class:`Session` via :func:`carrel.db.get_app_engine` when no session
    is passed in; we need that engine to be the test engine so the
    resolver finds (or doesn't find) overrides consistently.  Mirrors
    the same fixture in ``test_paper_extract`` / ``test_paper_card``.
    """
    import carrel.db as _db
    _db.app_engine = session.get_bind()


def test_summarize_paper_happy_path(session, cfg: CarrelYAML, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md")
    _write_md(cfg, p, "# Intro\n\nThis is the paper body. " * 20)
    monkeypatch.setattr(summ_pipe.llm, "chat_json", _fake_llm())

    out = summ_pipe.summarize_paper(session, cfg, p.id)
    session.refresh(out)

    assert out.status == PaperStatus.summarized.value
    assert out.tldr_en == "We propose a new method for X."
    assert out.tldr_zh == "我们提出了一种X的新方法。"
    assert "本文提出" in (out.summary_zh or "")
    assert out.keywords and "method" in out.keywords


def test_summarize_default_zh_marks_zh_fields_primary(session, cfg, tmp_path, monkeypatch):
    """cfg.llm.output_language defaults to zh; the directive must
    tell the LLM that 'tldr_zh' / 'summary_zh' are the primary
    output and 'tldr_en' is a gloss."""
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md")
    _write_md(cfg, p, "# Intro\n\nThis is the paper body. " * 20)

    captured: dict = {}

    def _capture(messages, **kwargs):
        for m in messages:
            if m["role"] == "system":
                captured["system"] = m["content"]
        return _fake_llm()(messages, **kwargs)

    monkeypatch.setattr(summ_pipe.llm, "chat_json", _capture)
    summ_pipe.summarize_paper(session, cfg, p.id)

    assert "tldr_zh" in captured["system"]
    assert "summary_zh" in captured["system"]
    assert "primary" in captured["system"]
    # The en-specific phrasing is absent so the directive switched.
    assert "primary output; populate it" not in captured["system"]


def test_summarize_english_marks_tldr_en_primary(session, cfg, tmp_path, monkeypatch):
    """Switching cfg.llm.output_language to 'en' is live on the next
    call: the directive names 'tldr_en' as the primary output."""
    cfg.llm.output_language = "en"
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md")
    _write_md(cfg, p, "# Intro\n\nThis is the paper body. " * 20)

    captured: dict = {}

    def _capture(messages, **kwargs):
        for m in messages:
            if m["role"] == "system":
                captured["system"] = m["content"]
        return _fake_llm()(messages, **kwargs)

    monkeypatch.setattr(summ_pipe.llm, "chat_json", _capture)
    summ_pipe.summarize_paper(session, cfg, p.id)

    assert "tldr_en" in captured["system"]
    assert "primary" in captured["system"]


def test_summarize_preserves_existing_s2_tldr(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(
        session, md_path="papers/W1/paper.md",
        tldr_en="Pre-existing S2 TL;DR.",
    )
    _write_md(cfg, p, "# Body\n\ncontent " * 20)
    monkeypatch.setattr(summ_pipe.llm, "chat_json", _fake_llm())

    out = summ_pipe.summarize_paper(session, cfg, p.id)
    session.refresh(out)

    # The S2-provided English TL;DR is preserved; other fields get filled.
    assert out.tldr_en == "Pre-existing S2 TL;DR."
    assert out.tldr_zh == "我们提出了一种X的新方法。"
    assert out.keywords


def test_summarize_is_idempotent(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(
        session, md_path="papers/W1/paper.md",
        status=PaperStatus.summarized.value,
        tldr_en="en", tldr_zh="zh", summary_zh="sum",
        keywords=["a", "b"],
    )
    _write_md(cfg, p, "# Body\n\ncontent " * 20)

    called = {"n": 0}

    def _chat(messages, **kwargs):
        called["n"] += 1
        return _fake_llm()(messages, **kwargs)

    monkeypatch.setattr(summ_pipe.llm, "chat_json", _chat)
    out = summ_pipe.summarize_paper(session, cfg, p.id)
    assert out.status == PaperStatus.summarized.value
    assert called["n"] == 0  # all fields present -> no LLM call


def test_summarize_force_overwrites(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(
        session, md_path="papers/W1/paper.md",
        status=PaperStatus.summarized.value,
        tldr_en="old en", tldr_zh="old zh", summary_zh="old sum",
        keywords=["old"],
    )
    _write_md(cfg, p, "# Body\n\ncontent " * 20)
    monkeypatch.setattr(summ_pipe.llm, "chat_json", _fake_llm(tldr_en="brand new"))

    out = summ_pipe.summarize_paper(session, cfg, p.id, force=True)
    session.refresh(out)
    assert out.tldr_en == "brand new"
    assert out.tldr_zh == "我们提出了一种X的新方法。"
    assert "old" not in (out.keywords or [])


def test_summarize_ready_paper_does_not_regress_status(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(
        session, md_path="papers/W1/paper.md",
        status=PaperStatus.ready.value,
    )
    _write_md(cfg, p, "# Body\n\ncontent " * 20)
    monkeypatch.setattr(summ_pipe.llm, "chat_json", _fake_llm())

    out = summ_pipe.summarize_paper(session, cfg, p.id)
    assert out.status == PaperStatus.ready.value  # stays ready
    assert out.tldr_en  # but fields get filled


def test_summarize_no_md_path_raises(session, cfg, tmp_path):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path=None)
    with pytest.raises(summ_pipe.SummarizeError, match="no md_path"):
        summ_pipe.summarize_paper(session, cfg, p.id)


def test_summarize_missing_md_file_raises(session, cfg, tmp_path):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/missing.md")
    with pytest.raises(summ_pipe.SummarizeError, match="missing on disk"):
        summ_pipe.summarize_paper(session, cfg, p.id)


def test_summarize_no_key_raises(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md")
    _write_md(cfg, p, "# Body\n\ncontent " * 20)
    monkeypatch.setattr(summ_pipe.llm, "has_key_for", lambda model: False)
    with pytest.raises(summ_pipe.SummarizeError, match="no LLM API key"):
        summ_pipe.summarize_paper(session, cfg, p.id)


def test_summarize_llm_failure_keeps_parsed(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md")
    _write_md(cfg, p, "# Body\n\ncontent " * 20)

    def _boom(messages, **kwargs):
        raise summ_pipe.llm.LLMError("upstream 500")

    monkeypatch.setattr(summ_pipe.llm, "chat_json", _boom)

    with pytest.raises(summ_pipe.SummarizeError):
        summ_pipe.summarize_paper(session, cfg, p.id)
    session.refresh(p)
    # Non-fatal: paper stays parsed (NOT failed), error field untouched.
    assert p.status == PaperStatus.parsed.value
    assert p.error is None


def test_summarize_dedupes_and_cleans_keywords(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, md_path="papers/W1/paper.md", tldr_en="kept", tldr_zh="kept")
    _write_md(cfg, p, "# Body\n\ncontent " * 20)
    monkeypatch.setattr(summ_pipe.llm, "chat_json", _fake_llm(
        keywords=["  RAG ", "rag.", "Retrieval", 42, "", "Graph"],
    ))
    out = summ_pipe.summarize_paper(session, cfg, p.id)
    # "RAG" and "rag." dedupe to one; non-strings/empty dropped; existing tldrs kept.
    assert out.tldr_en == "kept"
    kws = out.keywords or []
    assert "RAG" in kws
    assert sum(1 for k in kws if k.lower() == "rag") == 1
    assert 42 not in kws  # type: ignore[comparison-overlap]


def test_select_pending_summarize(session, cfg, tmp_path):
    cfg.storage.root = tmp_path / "data"
    # parsed, missing all fields -> eligible
    _make_paper(session, id="W-parsed", md_path="m.md", status=PaperStatus.parsed.value)
    # ready but missing summary (backfill case) -> eligible
    _make_paper(session, id="W-ready", md_path="m.md", status=PaperStatus.ready.value)
    # failed with md -> eligible
    _make_paper(session, id="W-failed", md_path="m.md", status=PaperStatus.failed.value)
    # already fully summarized -> NOT eligible
    _make_paper(
        session, id="W-done", md_path="m.md", status=PaperStatus.summarized.value,
        tldr_en="en", tldr_zh="zh", summary_zh="s", keywords=["k"],
    )
    # parsed but no markdown -> NOT eligible
    _make_paper(session, id="W-nomd", md_path=None, status=PaperStatus.parsed.value)
    # not in library -> NOT eligible
    _make_paper(
        session, id="W-inbox", md_path="m.md", status=PaperStatus.parsed.value,
        in_library=False,
    )

    rows = summ_pipe.select_pending_summarize(session, limit=20)
    ids = {r.id for r in rows}
    assert ids == {"W-parsed", "W-ready", "W-failed"}


def test_summarize_pending_batch_counts(session, cfg, tmp_path, monkeypatch):
    cfg.storage.root = tmp_path / "data"
    p1 = _make_paper(session, id="W1", md_path="papers/W1/paper.md")
    p2 = _make_paper(session, id="W2", md_path="papers/W2/paper.md")
    for p in (p1, p2):
        (cfg.storage.root / f"papers/{p.id}").mkdir(parents=True, exist_ok=True)
        (cfg.storage.root / f"papers/{p.id}/paper.md").write_text("body " * 20)

    def _chat(messages, **kwargs):
        return _fake_llm()(messages)

    monkeypatch.setattr(summ_pipe.llm, "chat_json", _chat)
    counts = summ_pipe.summarize_pending(session, cfg, limit=10)
    assert counts["candidates"] == 2
    assert counts["summarized"] == 2
    assert counts["failed"] == 0


def test_summarize_uses_section_picker(session, cfg, tmp_path, monkeypatch):
    """The body sent to the LLM is sliced by the section picker, not the
    old ``text[:max_chars]`` head-only trim.

    We give the paper a big Method section in the *middle* of the
    document.  The head-only trim would have cut it off; the picker
    recognises "## Methods" and keeps it under a small budget, so the
    LLM sees ``## [1] Method`` instead of a leading References /
    Acknowledgments block.
    """
    cfg.storage.root = tmp_path / "data"
    cfg.llm.max_input_chars = 1_500
    p = _make_paper(session, md_path="papers/W1/paper.md")
    method = "We propose a new method. " * 80  # ~2_000 chars
    md = (
        "# References\n\n"
        "[1] Foo, [2] Bar, [3] Baz. " * 100
        + "\n\n# Methods\n\n"
        + method
        + "\n\n# Acknowledgments\n\n"
        + "thanks " * 200
    )
    _write_md(cfg, p, md)

    captured: dict = {}

    def _chat(messages, **kwargs):
        for m in messages:
            if m["role"] == "user":
                captured.setdefault("bodies", []).append(m["content"])
        return _fake_llm()(messages, **kwargs)

    monkeypatch.setattr(summ_pipe.llm, "chat_json", _chat)
    summ_pipe.summarize_paper(session, cfg, p.id)

    body = captured["bodies"][-1]
    # The picker dropped References and Acknowledgments and emitted
    # the Method block as a numbered section.  The old head-only
    # trim would have shown the References / "[1] Foo" text first.
    assert "## [1] Method" in body
    assert "[1] Foo" not in body
    assert "thanks" not in body
