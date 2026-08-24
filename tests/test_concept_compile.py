from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlmodel import select

from carrel.config import CarrelYAML
from carrel.models import Paper, PaperConcept, WikiKind, WikiPage, WikiSource
from carrel.pipeline.wiki import _frontmatter
from carrel.pipeline.wiki import concept_compile as cc


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


def _add_concept(session, paper_id, term, *, display=None):
    pc = PaperConcept(
        paper_id=paper_id,
        term_normalized=term,
        term_display=display or term,
        evidence_quote="some quote",
    )
    session.add(pc)
    session.commit()


def _answer(summary="Grounded generation ties retrieval to answer quality [^1]."):
    return {"summary": summary, "tags": ["RAG", "Grounding"], "confidence": 0.9}


def _fakes(monkeypatch, answer=None):
    monkeypatch.setattr(cc.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(
        cc.llm, "chat_json",
        lambda messages, **kw: answer or _answer(),
    )
    monkeypatch.setattr(
        cc.embeddings, "embed_texts", lambda texts, model: [[0.0] * 2048]
    )


def test_single_paper_live_compile_no_stub(session, tmp_path, monkeypatch):
    # M8c: with the evidence threshold dropped to 1, a single-paper concept
    # gets a full LLM-compiled page (not a stub).  Stubs are reserved for
    # zero-evidence entries (impossible in practice — aggregation requires ≥1
    # backing paper — but kept for safety).
    cfg = _cfg(tmp_path)
    _paper(session, pid="W1")
    _add_concept(session, "W1", "retrieval-augmented generation", display="Retrieval-Augmented Generation")
    _fakes(monkeypatch)
    page = cc.compile_concept(
        session, cfg, "retrieval-augmented generation"
    )
    assert page.stub is False
    assert page.evidence_count == 1
    assert page.kind == WikiKind.concept.value
    full = tmp_path / page.path
    assert full.exists()
    meta, body = _frontmatter.parse(full.read_text())
    # Live pages do not emit a `stub:` key in the frontmatter (the DB row is
    # the source of truth for stub status).
    assert "stub" not in meta
    assert meta["evidence_count"] == 1
    assert "Not enough evidence" not in body


def test_threshold_met_live_compile_writes_file(session, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for i in range(3):
        pid = f"W{i+1}"
        _paper(session, pid=pid, title=f"RAG study {i+1}")
        _add_concept(
            session, pid, "retrieval-augmented generation",
            display="Retrieval-Augmented Generation",
        )
    _fakes(monkeypatch)
    page = cc.compile_concept(session, cfg, "retrieval-augmented generation")
    assert page.stub is False
    assert page.kind == WikiKind.concept.value
    assert page.slug == "retrieval-augmented-generation"
    assert page.evidence_count == 3
    full = tmp_path / page.path
    assert full.exists()
    meta, body = _frontmatter.parse(full.read_text())
    assert meta["kind"] == "concept"
    assert meta["entity_key"] == "concept:retrieval-augmented-generation"
    assert meta["evidence_count"] == 3
    assert meta["compiler_version"] == cc.COMPILER_VERSION
    assert meta["source_paper_ids"] == ["W1", "W2", "W3"]
    assert "evidence_hash" in meta
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
        _add_concept(
            session, pid, "retrieval-augmented generation",
            display="Retrieval-Augmented Generation",
        )
    _fakes(monkeypatch)
    first = cc.compile_concept(session, cfg, "retrieval-augmented generation")
    checksum = first.checksum

    called = []
    monkeypatch.setattr(
        cc.llm, "chat_json",
        lambda messages, **kw: called.append(1) or _answer(),
    )
    second = cc.compile_concept(session, cfg, "retrieval-augmented generation")
    assert called == []
    assert second.checksum == checksum
    assert cc.select_stale_concepts(session) == []


def test_evidence_growth_triggers_recompile(session, tmp_path, monkeypatch):
    # M8c: with the evidence threshold dropped to 1, every concept is live
    # from its first mention.  A growing paper set still re-compiles (the
    # evidence hash changes), so the "1 → 2 → 3" path replaces the old
    # "stub → live" promotion.
    cfg = _cfg(tmp_path)
    _paper(session, pid="W1")
    _add_concept(session, "W1", "retrieval-augmented generation", display="Retrieval-Augmented Generation")
    _fakes(monkeypatch)
    page = cc.compile_concept(session, cfg, "retrieval-augmented generation")
    assert page.stub is False
    first_hash = page.checksum

    # 1-paper page is up-to-date — not stale.
    assert "retrieval-augmented generation" not in cc.select_stale_concepts(session)

    _paper(session, pid="W2", title="RAG study two")
    _add_concept(session, "W2", "retrieval-augmented generation", display="Retrieval-Augmented Generation")
    # The evidence set grew → the page is now stale.
    assert "retrieval-augmented generation" in cc.select_stale_concepts(session)

    called = []
    monkeypatch.setattr(
        cc.llm, "chat_json",
        lambda messages, **kw: called.append(1) or _answer("Grown summary."),
    )
    page = cc.compile_concept(session, cfg, "retrieval-augmented generation")
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
        _add_concept(
            session, pid, "retrieval-augmented generation",
            display="Retrieval-Augmented Generation",
        )
    monkeypatch.setattr(cc.llm, "has_key_for", lambda model: False)
    with pytest.raises(cc.ConceptError, match="no LLM API key"):
        cc.compile_concept(session, cfg, "retrieval-augmented generation")


def test_batch_returns_all_compiled_at_threshold_one(session, tmp_path, monkeypatch):
    # M8c: with the evidence threshold dropped to 1, single-paper concepts
    # are live-compiled rather than stubbed.  The batch should report
    # 2 candidates, 2 compiled, 0 stubbed, 0 failed.
    cfg = _cfg(tmp_path)
    for i in range(3):
        pid = f"A{i+1}"
        _paper(session, pid=pid, title=f"Concept A paper {i+1}")
        _add_concept(session, pid, "concept a", display="Concept A")
    _paper(session, pid="B1", title="Concept B paper")
    _add_concept(session, "B1", "concept b", display="Concept B")
    _fakes(monkeypatch)
    counts = cc.compile_concepts_pending(session, cfg, limit=10)
    assert counts["candidates"] == 2
    assert counts["compiled"] == 2
    assert counts["stubbed"] == 0
    assert counts["failed"] == 0
    rows = session.exec(
        select(WikiPage).where(WikiPage.kind == WikiKind.concept.value)
    ).all()
    assert {r.slug for r in rows} == {"concept-a", "concept-b"}
    by_slug = {r.slug: r for r in rows}
    assert by_slug["concept-a"].stub is False
    assert by_slug["concept-b"].stub is False


def test_related_scholars_section_renders(session, tmp_path, monkeypatch):
    """A concept page must surface the contributing scholars as a deterministic
    '## Related scholars' footer with links into the scholar wiki."""
    cfg = _cfg(tmp_path)
    # Three papers from two distinct authors — the section should list both.
    for i, authors in enumerate([
        [{"name": "Alice Wu", "openalex_author_id": "A100", "affiliation": "X"}],
        [{"name": "Alice Wu", "openalex_author_id": "A100"}],
        [{"name": "Bob Tang", "openalex_author_id": "A200", "affiliation": "Y"}],
    ]):
        pid = f"W{i+1}"
        p = Paper(
            id=pid, id_kind="openalex", title=f"RAG study {i+1}",
            abstract="Retrieval improves answers.",
            tldr_en="Grounded generation helps factuality.",
            publication_date=date(2024, 1, 1),
            authors=authors,
            status="summarized", oa_status="oa", source="openalex", in_library=True,
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        )
        session.add(p)
        session.commit()
        _add_concept(
            session, pid, "retrieval-augmented generation",
            display="Retrieval-Augmented Generation",
        )
    _fakes(monkeypatch)
    page = cc.compile_concept(
        session, cfg, "retrieval-augmented generation"
    )
    full = tmp_path / page.path
    body = _frontmatter.parse(full.read_text())[1]
    assert "## Related scholars" in body
    # Alice Wu is on two papers → must sort first; Bob Tang second.
    alice_idx = body.find("[[Alice Wu]]")
    bob_idx = body.find("[[Bob Tang]]")
    assert alice_idx != -1 and bob_idx != -1
    assert alice_idx < bob_idx
    # Link target uses the scholar slug for the A-ID.
    assert "../scholars/A100.md" in body
    assert "../scholars/A200.md" in body


def test_related_scholars_omitted_when_no_authors(session, tmp_path, monkeypatch):
    """No authors on the contributing papers → no 'Related scholars' footer
    (the page is fine without it; the /scholars browse still surfaces the
    scholar rows)."""
    cfg = _cfg(tmp_path)
    p = Paper(
        id="W1", id_kind="openalex", title="Anon study",
        abstract="Anonymous.", tldr_en="Anonymized.",
        publication_date=date(2024, 1, 1),
        authors=[],
        status="summarized", oa_status="oa", source="openalex", in_library=True,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    session.add(p)
    session.commit()
    _add_concept(session, "W1", "retrieval-augmented generation",
                display="Retrieval-Augmented Generation")
    _fakes(monkeypatch)
    page = cc.compile_concept(session, cfg, "retrieval-augmented generation")
    body = _frontmatter.parse((tmp_path / page.path).read_text())[1]
    assert "## Related scholars" not in body