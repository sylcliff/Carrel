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
    assert batch.json()["kind"] == JobKind.wiki_compile.value
    assert batch.json()["status"] == "done" and batch.json()["stats"]["compiled"] >= 0


def test_inline_recompile_job(client, compiled):
    one = client.post(f"/wiki/pages/{compiled.id}/recompile", params={"background": False})
    assert one.status_code == 200
    assert one.json()["kind"] == JobKind.wiki_recompile.value and one.json()["status"] == "done"


def test_scholar_detail_includes_wiki_page(client, compiled):
    r = client.get("/scholars/A5013214678")
    assert r.status_code == 200, r.text
    wiki = r.json()["wiki_page"]
    assert wiki is not None and "# Jane Doe" in wiki["body"]
