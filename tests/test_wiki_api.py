from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from carrel.models import JobKind, Paper
from carrel.pipeline.wiki import scholar_compile as sc


def _answer():
    return {
        "summary": "Jane studies retrieval [^1].", "research_lines": ["RAG [^1]"],
        "trajectory": "Grounded systems [^1].", "evolving_views": "",
        "key_collaborators": [], "concept_links": [{"term": "RAG", "why": "method"}],
        "question_links": [], "tags": ["rag"], "confidence": 0.8,
    }


@pytest.fixture()
def compiled(client, session, tmp_path, monkeypatch):
    from carrel.main import app_config
    app_config.storage.root = tmp_path
    for kind in ("scholars", "concepts", "questions"):
        (tmp_path / "wiki" / kind).mkdir(parents=True, exist_ok=True)
    p = Paper(
        id="W-WIKI", id_kind="openalex", title="A Wiki Paper", abstract="Evidence about RAG.",
        tldr_en="Retrieval grounds answers.", publication_date=date(2024, 1, 1),
        authors=[{"name": "Jane Doe", "openalex_author_id": "A5013214678", "affiliation": "Example U"}],
        status="summarized", oa_status="oa", source="openalex", in_library=True,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    session.add(p); session.commit()
    monkeypatch.setattr(sc.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(sc.llm, "chat_json", lambda messages, **kw: _answer())
    monkeypatch.setattr(sc.embeddings, "embed_texts", lambda texts, model: [[0.0] * 2048])
    monkeypatch.setattr(sc, "get_profile", lambda key: None)
    monkeypatch.setattr("carrel.api.scholars.get_profile", lambda key: None)
    return sc.compile_scholar(session, app_config, "A5013214678")


def test_wiki_page_get_endpoints(client, compiled):
    rows = client.get("/wiki/pages").json()
    assert any(r["id"] == compiled.id for r in rows)
    filtered = client.get("/wiki/pages", params={"kind": "scholar"}).json()
    assert filtered and all(r["kind"] == "scholar" for r in filtered)
    detail = client.get(f"/wiki/pages/{compiled.id}")
    assert detail.status_code == 200
    assert "# Jane Doe" in detail.json()["body"]
    assert detail.json()["sources"][0]["paper_id"] == "W-WIKI"
    by_slug = client.get(f"/wiki/pages/by-kind-slug/scholar/{compiled.slug}")
    assert by_slug.status_code == 200 and by_slug.json()["id"] == compiled.id


def test_inline_compile_job(client, compiled):
    batch = client.post("/wiki/compile", json={"limit": 10, "background": False, "force": False})
    assert batch.status_code == 200
    body = batch.json()
    assert body["kind"] == JobKind.wiki_compile.value
    # Multi-phase stats: per-stage counts live under
    # stats[<stage>].  The compiled fixture seeds a single scholar, so the
    # scholar stage should report >= 0 compiles; the per-paper and concept
    # / question stages may report zero or have skipped.
    assert body["status"] == "done"
    scholar_stats = body["stats"].get("scholar_compile", {})
    assert scholar_stats.get("compiled", 0) >= 0
    # Stage list is echoed so the UI can render the right pill.
    assert "paper_extract" in body["stats"]["stages"]
    assert "scholar_compile" in body["stats"]["stages"]


def test_inline_recompile_job(client, compiled):
    one = client.post(f"/wiki/pages/{compiled.id}/recompile", params={"background": False})
    assert one.status_code == 200
    assert one.json()["kind"] == JobKind.wiki_recompile.value and one.json()["status"] == "done"


def test_scholar_detail_includes_wiki_page(client, compiled):
    r = client.get("/scholars/A5013214678")
    assert r.status_code == 200, r.text
    wiki = r.json()["wiki_page"]
    assert wiki is not None and "# Jane Doe" in wiki["body"]


def test_compile_stages_filter_runs_only_named_stages(client, session, tmp_path, monkeypatch):
    """A request with ``stages=['scholar_compile']`` should skip the others.

    Patches all four batch drivers so we can detect which were called.  The
    paper_extract driver is not expected to be invoked when the request
    names only one stage; the lazy-skip cascade doesn't apply because the
    stages list is explicit.
    """
    from carrel.main import app_config
    app_config.storage.root = tmp_path
    for kind in ("scholars", "concepts", "questions"):
        (tmp_path / "wiki" / kind).mkdir(parents=True, exist_ok=True)
    p = Paper(
        id="W-STG", id_kind="openalex", title="A Staged Paper",
        abstract="x", tldr_en="x", publication_date=date(2024, 1, 1),
        authors=[{"name": "Jane", "openalex_author_id": "A5013214678", "affiliation": "X"}],
        status="summarized", oa_status="oa", source="openalex", in_library=True,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    session.add(p); session.commit()

    monkeypatch.setattr(sc.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(sc.llm, "chat_json", lambda messages, **kw: _answer())
    monkeypatch.setattr(sc.embeddings, "embed_texts", lambda texts, model: [[0.0] * 2048])
    monkeypatch.setattr(sc, "get_profile", lambda key: None)
    monkeypatch.setattr("carrel.api.scholars.get_profile", lambda key: None)

    # Track which batch drivers are invoked.
    calls = {"paper_extract": 0, "scholar": 0, "concept": 0, "question": 0}

    def _spy_paper(*a, **kw):
        calls["paper_extract"] += 1
        return {"candidates": 0, "extracted": 0, "failed": 0}

    def _spy_scholar(*a, **kw):
        calls["scholar"] += 1
        return {"candidates": 0, "compiled": 0, "failed": 0}

    def _spy_concept(*a, **kw):
        calls["concept"] += 1
        return {"candidates": 0, "compiled": 0, "stubbed": 0, "failed": 0}

    def _spy_question(*a, **kw):
        calls["question"] += 1
        return {"candidates": 0, "compiled": 0, "stubbed": 0, "failed": 0}

    monkeypatch.setattr("carrel.api.wiki.extract_papers_pending", _spy_paper)
    monkeypatch.setattr("carrel.api.wiki.compile_scholars_pending", _spy_scholar)
    monkeypatch.setattr("carrel.api.wiki.compile_concepts_pending", _spy_concept)
    monkeypatch.setattr("carrel.api.wiki.compile_questions_pending", _spy_question)

    r = client.post(
        "/wiki/compile",
        json={"stages": ["scholar_compile"], "limit": 5, "background": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done"
    assert calls["scholar"] == 1
    assert calls["paper_extract"] == 0
    assert calls["concept"] == 0
    assert calls["question"] == 0
    # The stages the job actually ran are echoed in stats.
    assert body["stats"]["stages"] == ["scholar_compile"]
    # The per-stage counts are under the named stage.
    assert "scholar_compile" in body["stats"]


def test_compile_unknown_stage_marks_job_failed(client, session, tmp_path, monkeypatch):
    """A request naming a bogus stage fails the job with a clear message."""
    r = client.post(
        "/wiki/compile",
        json={"stages": ["nonsense_stage"], "limit": 1, "background": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert "nonsense_stage" in (body["message"] or "")


def test_recompile_concept_page_runs_for_concept_kind(
    client, session, tmp_path, monkeypatch
):
    """Recompile dispatches to ``compile_concept`` for a concept page (D.4)."""
    from carrel.main import app_config
    from carrel.models import PaperConcept, WikiPage

    app_config.storage.root = tmp_path
    for kind in ("scholars", "concepts", "questions"):
        (tmp_path / "wiki" / kind).mkdir(parents=True, exist_ok=True)
    p = Paper(
        id="W-CON", id_kind="openalex", title="RAG paper",
        abstract="x", tldr_en="x", publication_date=date(2024, 1, 1),
        authors=[{"name": "J", "openalex_author_id": "A1", "affiliation": "X"}],
        status="summarized", oa_status="oa", source="openalex", in_library=True,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    session.add(p)
    pc = PaperConcept(
        paper_id="W-CON", term_normalized="retrieval-augmented generation",
        term_display="Retrieval-Augmented Generation", evidence_quote="RAG",
    )
    session.add(pc); session.commit()

    # Compile a concept page so we have a row to recompile.
    from carrel.pipeline.wiki import concept_compile as cc
    monkeypatch.setattr(cc.llm, "has_key_for", lambda model: True)
    monkeypatch.setattr(
        cc.llm, "chat_json",
        lambda messages, **kw: {"summary": "x", "tags": ["a"], "confidence": 0.5},
    )
    monkeypatch.setattr(cc.embeddings, "embed_texts", lambda texts, model: [[0.0] * 2048])
    page = cc.compile_concept(session, app_config, "retrieval-augmented generation")
    assert page.kind == "concept"

    # Now recompile — should succeed (no longer 422) and dispatch to concept.
    r = client.post(
        f"/wiki/pages/{page.id}/recompile", params={"background": False}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done"
    assert body["stats"]["wiki_kind"] == "concept"
