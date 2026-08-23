from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlmodel import select

from carrel.config import CarrelYAML
from carrel.models import Paper, PaperQuestion, WikiKind, WikiPage, WikiSource
from carrel.pipeline.wiki import _frontmatter
from carrel.pipeline.wiki import question_compile as qc


def _cfg(tmp_path):
    cfg = CarrelYAML()
    cfg.storage.root = tmp_path
    for kind in ("scholars", "concepts", "questions"):
        (tmp_path / "wiki" / kind).mkdir(parents=True, exist_ok=True)
    return cfg


def _paper(session, *, pid="W1", title="RAG Study"):
    p = Paper(
        id=pid, id_kind="openalex", title=f"{title} {pid}",
        abstract="Retrieval improves answers with grounded context.",
        tldr_en="Grounded generation improves factuality.",
        publication_date=date(2024, 1, 1),
        authors=[{"name": "Jane Doe", "openalex_author_id": "A1", "affiliation": "X"}],
        status="summarized", oa_status="oa", source="openalex", in_library=True,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    session.add(p)
    session.commit()
    return p


def _add_question(session, paper_id, question, *, display=None):
    pq = PaperQuestion(
        paper_id=paper_id,
        question_normalized=question,
        question_display=display or question,
        evidence_quote="some quote",
    )
    session.add(pq)
    session.commit()


def _answer(summary="RAG drift on fast-moving corpora is a recurring open problem [^1].",
            why="Keeping retrieval fresh matters for factuality at scale.",
            confidence=0.9):
    return {
        "summary": summary,
        "why_it_matters": why,
        "confidence": confidence,
    }


def _fakes(monkeypatch, answer=None):
    monkeypatch.setattr(qc.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(
        qc.llm, "chat_json",
        lambda messages, **kw: answer or _answer(),
    )
    monkeypatch.setattr(
        qc.embeddings, "embed_texts", lambda texts, model: [[0.0] * 2048]
    )


def test_single_paper_live_compile_no_stub(session, tmp_path, monkeypatch):
    # M8c: with the evidence threshold dropped to 1, a single-paper question
    # gets a full LLM-compiled page (not a stub).  Stubs are reserved for
    # zero-evidence entries (impossible in practice — aggregation requires ≥1
    # backing paper — but kept for safety).
    cfg = _cfg(tmp_path)
    _paper(session, pid="W1")
    _add_question(
        session, "W1",
        "how can retrieval stay current with fast-moving knowledge bases",
        display="How can retrieval stay current with fast-moving knowledge bases?",
    )
    _fakes(monkeypatch)
    page = qc.compile_question(
        session, cfg, "how can retrieval stay current with fast-moving knowledge bases"
    )
    assert page.stub is False
    assert page.evidence_count == 1
    assert page.kind == WikiKind.question.value
    assert page.question_status == "open"
    full = tmp_path / page.path
    assert full.exists()
    meta, body = _frontmatter.parse(full.read_text())
    # Live pages do not emit a `stub:` key in the frontmatter (the DB row is
    # the source of truth for stub status).
    assert "stub" not in meta
    assert meta["evidence_count"] == 1
    assert meta["question_status"] == "open"
    assert "Not enough evidence" not in body


def test_threshold_met_live_compile_writes_file(session, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for i in range(3):
        pid = f"W{i+1}"
        _paper(session, pid=pid, title=f"RAG study {i+1}")
        _add_question(
            session, pid,
            "how can retrieval stay current with fast-moving knowledge bases",
            display="How can retrieval stay current with fast-moving knowledge bases?",
        )
    _fakes(monkeypatch)
    page = qc.compile_question(
        session, cfg, "how can retrieval stay current with fast-moving knowledge bases"
    )
    assert page.stub is False
    assert page.kind == WikiKind.question.value
    expected_slug = "how-can-retrieval-stay-current-with-fast-moving-knowledge-bases"
    assert page.slug == expected_slug
    assert page.evidence_count == 3
    assert page.question_status == "open"
    full = tmp_path / page.path
    assert full.exists()
    meta, body = _frontmatter.parse(full.read_text())
    assert meta["kind"] == "question"
    assert meta["entity_key"] == f"question:{expected_slug}"
    assert meta["evidence_count"] == 3
    assert meta["compiler_version"] == qc.COMPILER_VERSION
    assert meta["source_paper_ids"] == ["W1", "W2", "W3"]
    assert meta["question_status"] == "open"
    assert "evidence_hash" in meta
    assert "## Summary" in body
    assert "RAG drift on fast-moving corpora" in body
    assert "## Why it matters" in body
    assert "[^1]: [RAG study 1 W1](/papers/W1) (2024)" in body
    sources = session.exec(
        select(WikiSource).where(WikiSource.wiki_page_id == page.id)
    ).all()
    assert {s.paper_id for s in sources} == {"W1", "W2", "W3"}


def test_hash_skip_does_not_call_llm(session, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for i in range(3):
        pid = f"W{i+1}"
        _paper(session, pid=pid, title=f"RAG study {i+1}")
        _add_question(
            session, pid,
            "how can retrieval stay current with fast-moving knowledge bases",
            display="How can retrieval stay current with fast-moving knowledge bases?",
        )
    _fakes(monkeypatch)
    first = qc.compile_question(
        session, cfg, "how can retrieval stay current with fast-moving knowledge bases"
    )
    checksum = first.checksum

    called = []
    monkeypatch.setattr(
        qc.llm, "chat_json",
        lambda messages, **kw: called.append(1) or _answer(),
    )
    second = qc.compile_question(
        session, cfg, "how can retrieval stay current with fast-moving knowledge bases"
    )
    assert called == []
    assert second.checksum == checksum
    assert qc.select_stale_questions(session) == []


def test_evidence_growth_triggers_recompile(session, tmp_path, monkeypatch):
    # M8c: with the evidence threshold dropped to 1, every question is live
    # from its first mention.  A growing paper set still re-compiles (the
    # evidence hash changes), so the "1 → 2 → 3" path replaces the old
    # "stub → live" promotion.
    cfg = _cfg(tmp_path)
    _paper(session, pid="W1")
    _add_question(
        session, "W1",
        "how can retrieval stay current with fast-moving knowledge bases",
        display="How can retrieval stay current with fast-moving knowledge bases?",
    )
    _fakes(monkeypatch)
    page = qc.compile_question(
        session, cfg, "how can retrieval stay current with fast-moving knowledge bases"
    )
    assert page.stub is False
    first_hash = page.checksum

    # 1-paper page is up-to-date — not stale.
    assert (
        "how can retrieval stay current with fast-moving knowledge bases"
        not in qc.select_stale_questions(session)
    )

    _paper(session, pid="W2", title="RAG study two")
    _add_question(
        session, "W2",
        "how can retrieval stay current with fast-moving knowledge bases",
        display="How can retrieval stay current with fast-moving knowledge bases?",
    )
    # The evidence set grew → the page is now stale.
    assert (
        "how can retrieval stay current with fast-moving knowledge bases"
        in qc.select_stale_questions(session)
    )

    called = []
    monkeypatch.setattr(
        qc.llm, "chat_json",
        lambda messages, **kw: called.append(1) or _answer("Grown summary."),
    )
    page = qc.compile_question(
        session, cfg, "how can retrieval stay current with fast-moving knowledge bases"
    )
    assert called == [1]
    assert page.stub is False
    assert page.evidence_count == 2
    assert page.checksum != first_hash
    full = tmp_path / page.path
    assert "Grown summary" in full.read_text()


def test_no_key_raises(session, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for i in range(3):
        pid = f"W{i+1}"
        _paper(session, pid=pid, title=f"RAG study {i+1}")
        _add_question(
            session, pid,
            "how can retrieval stay current with fast-moving knowledge bases",
            display="How can retrieval stay current with fast-moving knowledge bases?",
        )
    monkeypatch.setattr(qc.llm, "has_key_for", lambda model: False)
    with pytest.raises(qc.QuestionError, match="no LLM API key"):
        qc.compile_question(
            session, cfg, "how can retrieval stay current with fast-moving knowledge bases"
        )


def test_batch_returns_all_compiled_at_threshold_one(session, tmp_path, monkeypatch):
    # M8c: with the evidence threshold dropped to 1, single-paper questions
    # are live-compiled rather than stubbed.  The batch should report
    # 2 candidates, 2 compiled, 0 stubbed, 0 failed.
    cfg = _cfg(tmp_path)
    for i in range(3):
        pid = f"A{i+1}"
        _paper(session, pid=pid, title=f"Question A paper {i+1}")
        _add_question(session, pid, "what is question a", display="What is question A?")
    _paper(session, pid="B1", title="Question B paper")
    _add_question(session, "B1", "what is question b", display="What is question B?")
    _fakes(monkeypatch)
    counts = qc.compile_questions_pending(session, cfg, limit=10)
    assert counts["candidates"] == 2
    assert counts["compiled"] == 2
    assert counts["stubbed"] == 0
    assert counts["failed"] == 0
    rows = session.exec(
        select(WikiPage).where(WikiPage.kind == WikiKind.question.value)
    ).all()
    assert {r.slug for r in rows} == {"what-is-question-a", "what-is-question-b"}
    by_slug = {r.slug: r for r in rows}
    assert by_slug["what-is-question-a"].stub is False
    assert by_slug["what-is-question-a"].question_status == "open"
    assert by_slug["what-is-question-b"].stub is False
    assert by_slug["what-is-question-b"].question_status == "open"
