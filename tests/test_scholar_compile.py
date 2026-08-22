from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from carrel.config import CarrelYAML
from carrel.models import Paper, WikiKind, WikiPage, WikiSource
from carrel.pipeline.wiki import _frontmatter
from carrel.pipeline.wiki import scholar_compile as sc
from sqlmodel import select


def _cfg(tmp_path):
    cfg = CarrelYAML()
    cfg.storage.root = tmp_path
    for kind in ("scholars", "concepts", "questions"):
        (tmp_path / "wiki" / kind).mkdir(parents=True, exist_ok=True)
    return cfg


def _paper(session, *, pid="W1", aid="A5013214678", name="Jane Doe"):
    p = Paper(
        id=pid, id_kind="openalex", title=f"RAG Study {pid}", abstract="Retrieval improves answers.",
        tldr_en="Grounded generation improves factuality.", publication_date=date(2024, 1, 1),
        authors=[{"name": name, "openalex_author_id": aid, "affiliation": "Example U"}],
        status="summarized", oa_status="oa", source="openalex", in_library=True,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    session.add(p); session.commit(); return p


def _answer(summary="Jane studies grounded generation."):
    return {
        "summary": summary + " [^1]", "research_lines": ["Retrieval methods [^1]"],
        "trajectory": "Her work develops grounded systems [^1].", "evolving_views": "",
        "key_collaborators": [{"name": "John Smith", "aid": "A2", "reason": "coauthor"}],
        "concept_links": [{"term": "Retrieval-Augmented Generation", "why": "central method"}],
        "question_links": [{"question": "How can retrieval stay current?", "why": "raised in evidence"}],
        "tags": ["RAG", "Grounding"], "confidence": 0.95,
    }


def _fakes(monkeypatch, answer=None):
    monkeypatch.setattr(sc.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(sc.llm, "chat_json", lambda messages, **kw: answer or _answer())
    monkeypatch.setattr(sc.embeddings, "embed_texts", lambda texts, model: [[0.0] * 2048])
    monkeypatch.setattr(sc, "get_profile", lambda key: None)


def test_compile_scholar_writes_indexes_sources_and_is_idempotent(session, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path); _paper(session); _fakes(monkeypatch)
    assert sc.select_stale_scholars(session) == ["A5013214678"]
    page = sc.compile_scholar(session, cfg, "A5013214678")
    full = tmp_path / "wiki/scholars/A5013214678.md"
    assert full.exists()
    meta, body = _frontmatter.parse(full.read_text())
    assert meta["kind"] == "scholar" and meta["slug"] == "A5013214678"
    assert meta["openalex_id"] == "A5013214678"
    assert meta["compiler_version"] == sc.COMPILER_VERSION
    assert meta["source_paper_ids"] == ["W1"]
    assert "[[Retrieval-Augmented Generation]](../concepts/retrieval-augmented-generation.md)" in body
    assert "## Sources" in body and "[^1]: [RAG Study W1](/papers/W1) (2024)" in body
    assert page.kind == WikiKind.scholar.value and page.scholar_aid == "A5013214678"
    assert page.title == "Jane Doe" and page.confidence <= 0.45 and page.evidence_count == 1
    sources = session.exec(select(WikiSource).where(WikiSource.wiki_page_id == page.id)).all()
    assert len(sources) == 1 and sources[0].paper_id == "W1"
    assert sources[0].chunk_id is None and sources[0].heading == "abstract"
    checksum = page.checksum
    monkeypatch.setattr(sc.llm, "chat_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    same = sc.compile_scholar(session, cfg, "A5013214678")
    assert same.checksum == checksum
    assert sc.select_stale_scholars(session) == []


def test_name_only_confidence_is_lower(session, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path); _paper(session, aid="", name="Jane Doe"); _fakes(monkeypatch)
    page = sc.compile_scholar(session, cfg, "name:Jane Doe")
    assert page.slug == "name--jane-doe" and page.scholar_aid is None
    assert page.confidence < 0.45


def test_force_recompiles_and_preserves_user_section(session, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path); _paper(session); _fakes(monkeypatch)
    page = sc.compile_scholar(session, cfg, "A5013214678")
    old_checksum = page.checksum
    path = tmp_path / page.path
    text = path.read_text().replace("<!-- Your notes on this page. The compiler preserves everything inside this section. -->", "MY NOTES")
    path.write_text(text)
    assert "MY NOTES" in path.read_text()
    calls = []
    monkeypatch.setattr(sc.llm, "chat_json", lambda messages, **kw: calls.append(1) or _answer("Updated summary."))
    updated = sc.compile_scholar(session, cfg, "A5013214678", force=True)
    assert calls == [1] and "MY NOTES" in path.read_text()
    assert updated.checksum != old_checksum and "Updated summary" in path.read_text()


def test_no_key_raises(session, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path); _paper(session)
    monkeypatch.setattr(sc.llm, "has_key_for", lambda model: False)
    with pytest.raises(sc.ScholarError, match="no LLM API key"):
        sc.compile_scholar(session, cfg, "A5013214678")
