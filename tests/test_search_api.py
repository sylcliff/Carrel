"""API tests for the multi-source /search endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from carrel.sources import merge as m
from carrel.sources.merge import MutableSearchHit as Hit


def _oa_work(
    w_id="W1", doi="10.1/a", arxiv=None, title="Paper A",
    cited=10, year="2024-05-01", venue="NeurIPS",
):
    return {
        "id": f"https://openalex.org/{w_id}",
        "title": title,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "publication_date": year,
        "publication_year": int(year[:4]) if year else None,
        "cited_by_count": cited,
        "primary_location": {
            "source": {"display_name": venue, "type": "conference"}
        },
        "authorships": [
            {"author": {"display_name": "Alice"}, "institutions": []},
        ],
        "abstract_inverted_index": None,
        "open_access": {"is_oa": False},
        "ids": {"arxiv": arxiv} if arxiv else {},
    }


def _s2_row(
    s2_id="s2abc", doi="10.1/a", arxiv=None, title="Paper A",
    cited=12, year="2024-05-01", venue="NeurIPS", tldr="Short.",
):
    return {
        "s2_paper_id": s2_id,
        "title": title,
        "abstract": None,
        "authors": ["A. Researcher"],
        "venue": venue,
        "venue_type": "conference",
        "publication_date": year,
        "doi": doi,
        "arxiv_id": arxiv,
        "pdf_url": None,
        "citation_count": cited,
        "tldr": tldr,
        "source": "semantic_scholar",
    }


def _arxiv_entry(arxiv_id="2401.00001", title="Paper A", summary="abstract"):
    from carrel.sources.arxiv import ArxivEntry
    return ArxivEntry(
        arxiv_id=arxiv_id, title=title, summary=summary,
        authors=["Alice", "Bob"], categories=["cs.CL"],
        updated="2024-05-01T00:00:00Z",
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
    )


def _crossref_row(doi="10.1/a", title="Paper A", year="2024-05-12", venue="Nature",
                  type_="journal-article", arxiv=None, pdf=True):
    """A raw Crossref message-item dict shaped like the API returns it."""
    from_published = [int(year[:4])]
    if len(year) >= 7:
        from_published.append(int(year[5:7]))
    if len(year) >= 10:
        from_published.append(int(year[8:10]))
    link = []
    if pdf:
        link.append({
            "URL": f"https://example.com/{doi.replace('/', '_')}.pdf",
            "content-version": "vor",
            "content-type": "application/pdf",
        })
    doi_value = arxiv if arxiv else doi
    return {
        "DOI": doi_value,
        "title": [title],
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "container-title": [venue],
        "type": type_,
        "abstract": "<jats:p>An abstract.</jats:p>",
        "published-print": {"date-parts": [from_published]},
        "link": link,
    }


def _patch_sources(monkeypatch, *, oa_rows=None, s2_rows=None, arxiv_entries=None,
                   cr_rows=None,
                   oa_raises=None, s2_raises=None, arxiv_raises=None, cr_raises=None):
    import carrel.api.search as search_mod

    def fake_oa_search(*a, **kw):
        if oa_raises:
            raise oa_raises
        return list(oa_rows or [])

    def fake_s2_search(*a, **kw):
        if s2_raises:
            raise s2_raises
        return list(s2_rows or [])

    def fake_arxiv_search(*a, **kw):
        if arxiv_raises:
            raise arxiv_raises
        return list(arxiv_entries or [])

    def fake_cr_search(*a, **kw):
        if cr_raises:
            raise cr_raises
        return list(cr_rows or [])

    monkeypatch.setattr(search_mod.oa, "search_work", fake_oa_search)
    monkeypatch.setattr(search_mod.s2, "search_papers", fake_s2_search)
    monkeypatch.setattr(search_mod.arxiv_src, "search", fake_arxiv_search)
    monkeypatch.setattr(search_mod.cr, "search_papers", fake_cr_search)


def test_combined_search_merges_oa_and_s2_by_doi(session, client: TestClient, monkeypatch):
    _patch_sources(
        monkeypatch,
        oa_rows=[_oa_work()],
        s2_rows=[_s2_row()],
        arxiv_entries=[],
    )
    r = client.get("/search", params={"q": "rag"})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "rag"
    assert body["warnings"] == []
    assert len(body["results"]) == 1
    item = body["results"][0]
    assert set(item["sources"]) == {"openalex", "semantic_scholar"}
    assert item["ids"]["openalex"] == "W1"
    assert item["ids"]["doi"] == "10.1/a"
    assert item["ids"]["s2"] == "s2abc"
    assert item["citation_count"] == 12  # max of 10 and 12
    assert item["tldr"] == "Short."


def test_combined_search_arxiv_contributes_pdf(session, client: TestClient, monkeypatch):
    _patch_sources(
        monkeypatch,
        oa_rows=[_oa_work(arxiv="2401.00001")],
        s2_rows=[_s2_row(arxiv="2401.00001")],
        arxiv_entries=[_arxiv_entry()],
    )
    r = client.get("/search", params={"q": "rag"})
    item = r.json()["results"][0]
    assert set(item["sources"]) == {"openalex", "semantic_scholar", "arxiv"}
    assert item["pdf_url"] == "https://arxiv.org/pdf/2401.00001"


def test_partial_source_failure_returns_other_results(session, client: TestClient, monkeypatch):
    _patch_sources(
        monkeypatch,
        oa_rows=[_oa_work(w_id="W2", doi="10.1/b", title="OA only")],
        s2_raises=RuntimeError("timeout"),
        arxiv_entries=[_arxiv_entry(arxiv_id="2402.00002", title="Arxiv only")],
    )
    r = client.get("/search", params={"q": "x"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) == 2
    assert any("semantic_scholar" in w for w in body["warnings"])


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------


def test_crossref_only_hit_returns_one_result(session, client: TestClient, monkeypatch):
    _patch_sources(
        monkeypatch,
        cr_rows=[_crossref_row(doi="10.99/c", title="Crossref only", year="2024-05-12")],
    )
    r = client.get("/search", params={"q": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["warnings"] == []
    assert len(body["results"]) == 1
    item = body["results"][0]
    assert item["sources"] == ["crossref"]
    assert item["ids"]["doi"] == "10.99/c"
    assert item["venue"] == "Nature"
    assert item["venue_type"] == "journal"
    # PDF URL picked from the Crossref link[].url.
    assert item["pdf_url"] == "https://example.com/10.99_c.pdf"


def test_crossref_merges_with_oa_on_same_doi(session, client: TestClient, monkeypatch):
    _patch_sources(
        monkeypatch,
        oa_rows=[_oa_work(doi="10.1/a", title="Shared")],
        cr_rows=[_crossref_row(doi="10.1/a", title="Shared", year="2024-05-12")],
    )
    r = client.get("/search", params={"q": "x"})
    body = r.json()
    assert len(body["results"]) == 1
    item = body["results"][0]
    assert set(item["sources"]) == {"openalex", "crossref"}
    # DOI is the merge key.
    assert item["ids"]["doi"] == "10.1/a"
    assert item["ids"]["openalex"] == "W1"


def test_crossref_failure_returns_other_results_with_warning(
    session, client: TestClient, monkeypatch
):
    _patch_sources(
        monkeypatch,
        oa_rows=[_oa_work()],
        cr_raises=RuntimeError("crossref 5xx"),
    )
    r = client.get("/search", params={"q": "x"})
    body = r.json()
    assert len(body["results"]) == 1
    assert any("crossref" in w for w in body["warnings"])


def test_crossref_zenodo_doi_is_filtered(session, client: TestClient, monkeypatch):
    """Zenodo deposits are software/datasets, not papers; they should be
    excluded by the endpoint's is_zenodo() check.
    """
    _patch_sources(
        monkeypatch,
        cr_rows=[_crossref_row(doi="10.5281/zenodo.12345", title="Zenodo dep", pdf=False)],
    )
    r = client.get("/search", params={"q": "x"})
    body = r.json()
    assert body["results"] == []


def test_crossref_year_filter_post_filtered(session, client: TestClient, monkeypatch):
    """Defense in depth: the year filter is applied post-fetch too, in case
    a Crossref row has no published-print block.
    """
    _patch_sources(
        monkeypatch,
        cr_rows=[
            _crossref_row(doi="10.99/old", title="Old", year="2015-01-01"),
            _crossref_row(doi="10.99/new", title="New", year="2024-01-01"),
        ],
    )
    r = client.get("/search", params={"q": "x", "year_from": 2020})
    body = r.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["ids"]["doi"] == "10.99/new"


def test_crossref_arxiv_doi_merges_with_arxiv_id(session, client: TestClient, monkeypatch):
    """A Crossref hit with a 10.48550/arXiv.* DOI should carry the bare
    arXiv id so the merge layer can collide it with the arXiv source.
    """
    _patch_sources(
        monkeypatch,
        arxiv_entries=[_arxiv_entry(arxiv_id="2401.00001", title="arXiv version")],
        cr_rows=[_crossref_row(arxiv="10.48550/arXiv.2401.00001v2",
                                title="arXiv version", pdf=False)],
    )
    r = client.get("/search", params={"q": "x"})
    body = r.json()
    assert len(body["results"]) == 1
    item = body["results"][0]
    assert set(item["sources"]) == {"arxiv", "crossref"}
    assert item["ids"]["arxiv"] == "2401.00001"


def test_facets_propagate_to_sources(session, client: TestClient, monkeypatch):
    seen = {}

    def fake_oa(query, **kw):
        seen["oa"] = kw
        return [_oa_work()]

    def fake_s2(query, **kw):
        seen["s2"] = kw
        return [_s2_row()]

    def fake_arxiv(query, **kw):
        seen["arxiv"] = kw
        return []

    import carrel.api.search as search_mod
    monkeypatch.setattr(search_mod.oa, "search_work", fake_oa)
    monkeypatch.setattr(search_mod.s2, "search_papers", fake_s2)
    monkeypatch.setattr(search_mod.arxiv_src, "search", fake_arxiv)

    client.get("/search", params={
        "q": "x",
        "year_from": 2020, "year_to": 2024,
        "min_citations": 10, "open_access_only": "true",
        "sort": "citations",
        "sources": ["openalex", "semantic_scholar"],
    })
    assert seen["oa"]["year_from"] == 2020
    assert seen["oa"]["min_citations"] == 10
    assert seen["oa"]["sort"] == "citations"
    assert seen["s2"]["year_from"] == 2020
    assert seen["s2"]["open_access_only"] is True
    # arXiv was excluded via sources filter.
    assert "arxiv" not in seen


def test_sort_by_citations_orders_desc(session, client: TestClient, monkeypatch):
    _patch_sources(
        monkeypatch,
        oa_rows=[
            _oa_work(w_id="W1", doi="10.1/a", title="Low", cited=1),
            _oa_work(w_id="W2", doi="10.1/b", title="High", cited=100),
        ],
    )
    r = client.get("/search", params={"q": "x", "sort": "citations"})
    titles = [x["title"] for x in r.json()["results"]]
    assert titles == ["High", "Low"]


def test_library_paper_merges_with_external_hit(session, client: TestClient, monkeypatch):
    # Seed a library paper with the same DOI as the OA hit.
    from datetime import date
    from carrel.models import Paper, PaperStatus
    session.add(Paper(
        id="W1", id_kind="openalex", title="Paper A",
        abstract=None, publication_date=date(2024, 5, 1),
        doi="https://doi.org/10.1/a", arxiv_id=None,
        source="openalex", status=PaperStatus.parsed.value,
        citation_count=20,
        authors=[{"name": "Lib Author"}],
        created_at=date(2024, 5, 1), updated_at=date(2024, 5, 1),
    ))
    session.commit()

    _patch_sources(monkeypatch, oa_rows=[_oa_work()], s2_rows=[], arxiv_entries=[])
    r = client.get("/search", params={"q": "paper"})
    results = r.json()["results"]
    assert len(results) == 1
    item = results[0]
    assert item["in_library"] is True
    assert item["library_id"] == "W1"
    assert item["status"] == "parsed"
    assert "library" in item["sources"]


def test_external_hit_matched_in_library_gets_library_source_tag(
    session, client: TestClient, monkeypatch,
):
    """An external-only hit (not returned by local ILIKE) whose DOI matches a
    library paper must still carry the 'library' source badge after the
    batched membership lookup."""
    from datetime import date
    from carrel.models import Paper, PaperStatus
    session.add(Paper(
        id="W9", id_kind="openalex", title="A Completely Different Title",
        abstract=None, publication_date=date(2024, 5, 1),
        doi="https://doi.org/10.1/a", arxiv_id=None,
        source="openalex", status=PaperStatus.parsed.value,
        citation_count=20, authors=[],
        created_at=date(2024, 5, 1), updated_at=date(2024, 5, 1),
    ))
    session.commit()

    # The local ILIKE query searches for "paper" and won't find a paper titled
    # "A Completely Different Title", so only the external source emits it.
    _patch_sources(monkeypatch, oa_rows=[_oa_work()], s2_rows=[], arxiv_entries=[])
    r = client.get("/search", params={"q": "paper"})
    item = r.json()["results"][0]
    assert item["in_library"] is True
    assert item["library_id"] == "W9"
    assert "library" in item["sources"]
    assert "openalex" in item["sources"]


def test_search_external_routes_and_local_routes(session, client: TestClient, monkeypatch):
    _patch_sources(monkeypatch, oa_rows=[_oa_work()], s2_rows=[], arxiv_entries=[])
    r = client.get("/search/external", params={"q": "rag"})
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = client.get("/search/local", params={"q": "nothingmatches"})
    assert r.status_code == 200
    assert r.json() == []


def test_spelling_correction_wraps_sources(session, client: TestClient, monkeypatch):
    seen_q = {}

    def fake_oa(query, **kw):
        seen_q["oa"] = query
        return [_oa_work()]

    def fake_s2(query, **kw):
        seen_q["s2"] = query
        return []

    def fake_arxiv(query, **kw):
        seen_q["arxiv"] = query
        return []

    import carrel.api.search as search_mod
    monkeypatch.setattr(search_mod.oa, "search_work", fake_oa)
    monkeypatch.setattr(search_mod.s2, "search_papers", fake_s2)
    monkeypatch.setattr(search_mod.arxiv_src, "search", fake_arxiv)

    # Seed spelling so it has a chance to correct; use a known typo "transfomer"
    # which the library-dictionary + bundled dictionary corrects.
    r = client.get("/search", params={"q": "transformer"})
    assert r.status_code == 200
    # No assertion on correction happening; just ensure sources all got the
    # same (possibly corrected) query.
    assert seen_q["oa"] == r.json()["query"]
    assert seen_q["s2"] == seen_q["oa"]
    assert seen_q["arxiv"] == seen_q["oa"]


def test_import_via_s2_resolves_to_paper(session, client: TestClient, monkeypatch):
    """POST /import with body={s2} looks up S2 then resolves DOI via OpenAlex."""
    import carrel.api.search as search_mod

    s2_row = _s2_row(doi="10.1/a", arxiv=None)

    def fake_fetch_paper(s2_id, **kw):
        assert s2_id == "s2abc"
        return s2_row

    def fake_lookup_doi(doi):
        assert doi == "10.1/a"
        return _oa_work()

    monkeypatch.setattr(search_mod.s2, "fetch_paper", fake_fetch_paper)
    monkeypatch.setattr(search_mod.oa, "lookup_by_doi", fake_lookup_doi)

    r = client.post("/import", json={"s2": "s2abc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["id"] == "W1"


def test_import_s2_only_when_openalex_has_nothing(session, client, monkeypatch):
    """When OA DOI/arXiv/title all miss, import creates an s2: paper from S2."""
    import carrel.api.search as search_mod

    s2_row = _s2_row(s2_id="s2zzz", doi=None, arxiv=None, title="Orphan paper",
                     venue="Some Workshop", year="2021-03-01")
    s2_row["pdf_url"] = "https://example.org/p.pdf"

    monkeypatch.setattr(search_mod.s2, "fetch_paper", lambda *a, **k: s2_row)
    monkeypatch.setattr(search_mod.oa, "lookup_by_doi", lambda *a, **k: None)
    monkeypatch.setattr(search_mod.oa, "lookup_by_arxiv_id", lambda *a, **k: None)
    monkeypatch.setattr(search_mod.oa, "search_work", lambda *a, **k: [])

    r = client.post("/import", json={"s2": "s2zzz"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["id"] == "s2:s2zzz"

    from carrel.models import Paper
    paper = session.get(Paper, "s2:s2zzz")
    assert paper is not None
    assert paper.id_kind == "semanticscholar"
    assert paper.s2_paper_id == "s2zzz"
    assert paper.pdf_url == "https://example.org/p.pdf"
    assert paper.title == "Orphan paper"


def test_import_s2_only_is_idempotent(session, client, monkeypatch):
    """Re-importing an s2 paper returns the existing row (created=False)."""
    import carrel.api.search as search_mod

    s2_row = _s2_row(s2_id="s2dup", doi=None, arxiv=None, title="Dup")
    monkeypatch.setattr(search_mod.s2, "fetch_paper", lambda *a, **k: s2_row)
    monkeypatch.setattr(search_mod.oa, "lookup_by_doi", lambda *a, **k: None)
    monkeypatch.setattr(search_mod.oa, "lookup_by_arxiv_id", lambda *a, **k: None)
    monkeypatch.setattr(search_mod.oa, "search_work", lambda *a, **k: [])

    r1 = client.post("/import", json={"s2": "s2dup"})
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"id": "s2:s2dup", "created": True}

    r2 = client.post("/import", json={"s2": "s2dup"})
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"id": "s2:s2dup", "created": False}
