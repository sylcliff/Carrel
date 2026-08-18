"""Tests for the citation API: list endpoint with library match + refresh job."""
from __future__ import annotations

from datetime import UTC, datetime

from carrel.models import Job, Paper, PaperStatus


def _now():
    return datetime.now(UTC)


def _seed_paper(session, **kw) -> str:
    base = dict(
        id="W100",
        id_kind="openalex",
        title="Cited Paper",
        status=PaperStatus.parsed.value,
        oa_status="oa",
        source="openalex",
        doi="https://doi.org/10.1000/cited",
        created_at=_now(),
        updated_at=_now(),
        citation_count=2,
        influential_citation_count=1,
        reference_count=10,
        s2_paper_id="s2-cited",
        citations_updated_at=_now(),
        citing_papers=[
            {"title": "In Library", "year": 2024, "doi": "10.2000/inlib",
             "arxiv_id": None, "s2_paper_id": "s2-inlib"},
            {"title": "Outside", "year": 2023, "doi": "10.3000/out",
             "arxiv_id": None, "s2_paper_id": "s2-out"},
        ],
    )
    base.update(kw)
    session.add(Paper(**base))
    session.commit()
    return base["id"]


def test_list_citations_resolves_library_membership(client, session):
    pid = _seed_paper(session)
    # A paper in the library whose DOI matches the first citing item.
    session.add(Paper(
        id="W200", id_kind="openalex", title="In Library",
        status=PaperStatus.ready.value, oa_status="oa", source="openalex",
        doi="https://doi.org/10.2000/inlib", created_at=_now(), updated_at=_now(),
    ))
    session.commit()

    r = client.get(f"/papers/{pid}/citations")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["paper_id"] == pid
    assert data["citation_count"] == 2
    assert data["influential_citation_count"] == 1
    assert len(data["citing"]) == 2

    in_lib = next(c for c in data["citing"] if c["title"] == "In Library")
    assert in_lib["in_library"] is True
    assert in_lib["paper_id"] == "W200"

    outside = next(c for c in data["citing"] if c["title"] == "Outside")
    assert outside["in_library"] is False
    assert outside["paper_id"] is None


def test_list_citations_matches_by_arxiv_id(client, session):
    pid = _seed_paper(session, id="W101", doi=None,
                      citing_papers=[{"title": "Arxiv Cite", "year": 2024,
                                      "doi": None, "arxiv_id": "2401.00099",
                                      "s2_paper_id": "s2-x"}])
    session.add(Paper(
        id="arxiv:2401.00099", id_kind="arxiv", title="Arxiv Cite",
        status=PaperStatus.parsed.value, oa_status="oa", source="arxiv",
        arxiv_id="2401.00099", created_at=_now(), updated_at=_now(),
    ))
    session.commit()

    r = client.get(f"/papers/{pid}/citations")
    assert r.status_code == 200
    item = r.json()["citing"][0]
    assert item["in_library"] is True
    assert item["paper_id"] == "arxiv:2401.00099"


def test_list_citations_404(client):
    r = client.get("/papers/nope/citations")
    assert r.status_code == 404


def test_refresh_creates_job_and_enriches(client, session, monkeypatch):
    pid = _seed_paper(session, citation_count=None, citing_papers=None,
                      citations_updated_at=None)

    def fake_enrich(sess, cfg, paper_id, *, on_progress=None, **kw):
        if on_progress:
            on_progress({"stage": "citations", "detail": "Querying…"})
        p = sess.get(Paper, paper_id)
        p.citation_count = 99
        p.citations_updated_at = _now()
        sess.add(p)
        sess.commit()
        return True

    monkeypatch.setattr("carrel.api.citations.enrich_paper", fake_enrich)

    r = client.post(f"/papers/{pid}/refresh-citations", json={"background": False})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "citations"
    assert job["status"] == "done"
    assert job["stats"]["paper_id"] == pid

    # Paper was updated by the (stubbed) enrichment.
    r2 = client.get(f"/papers/{pid}")
    assert r2.json()["citation_count"] == 99


def test_list_citations_with_sort_and_offset_paginates_cache(client, session):
    """?sort=year_asc&offset=N&limit=K returns the K items at offset N in
    ascending-year order, with next_offset chaining to the next page."""
    pid = _seed_paper(
        session, id="W103", citation_count=10,
        citing_papers=[
            {"title": "B 2020", "year": 2020, "doi": None, "arxiv_id": None,
             "s2_paper_id": "s2-b", "openalex_id": None},
            {"title": "A 2018", "year": 2018, "doi": None, "arxiv_id": None,
             "s2_paper_id": "s2-a", "openalex_id": None},
            {"title": "C 2022", "year": 2022, "doi": None, "arxiv_id": None,
             "s2_paper_id": "s2-c", "openalex_id": None},
        ],
    )

    r = client.get(f"/papers/{pid}/citations?sort=year_asc&offset=0&limit=2")
    assert r.status_code == 200
    d = r.json()
    titles = [c["title"] for c in d["citing"]]
    assert titles == ["A 2018", "B 2020"], f"asc sort: got {titles}"
    assert d["next_offset"] == 2
    assert d["source"] == "cache"

    r2 = client.get(f"/papers/{pid}/citations?sort=year_asc&offset=2&limit=2")
    d2 = r2.json()
    assert [c["title"] for c in d2["citing"]] == ["C 2022"]
    assert d2["next_offset"] is None, "last page -> no next_offset"


def test_list_citations_year_desc_sorts_in_place(client, session):
    pid = _seed_paper(
        session, id="W104", citation_count=3,
        citing_papers=[
            {"title": "Old", "year": 2018, "doi": None, "arxiv_id": None,
             "s2_paper_id": "s1"},
            {"title": "New", "year": 2024, "doi": None, "arxiv_id": None,
             "s2_paper_id": "s2"},
            {"title": "Mid", "year": 2021, "doi": None, "arxiv_id": None,
             "s2_paper_id": "s3"},
        ],
    )
    r = client.get(f"/papers/{pid}/citations?sort=year_desc&offset=0&limit=10")
    titles = [c["title"] for c in r.json()["citing"]]
    assert titles == ["New", "Mid", "Old"]


def test_list_citations_offset_past_cache_falls_through_to_openalex(
    client, session, monkeypatch,
):
    """When the user scrolls past the cached list, the endpoint must hit
    OpenAlex live and tag the response with source='openalex'."""
    pid = _seed_paper(
        session, id="W105", id_kind="openalex", citation_count=9999,
        citing_papers=[
            {"title": "Cached", "year": 2020, "doi": None, "arxiv_id": None,
             "s2_paper_id": "s2-cached", "openalex_id": None},
        ],
    )

    class _Page:
        def get(self, per_page, page):
            return [
                {"id": f"https://openalex.org/W{800+page}", "title": f"Live p{page} #{i}",
                 "publication_year": 2020 + i, "doi": None}
                for i in range(per_page)
            ]
    class _Works:
        def filter(self, **kw): return self
        def sort(self, **kw): return self
        def get(self, per_page, page): return _Page().get(per_page, page)
    monkeypatch.setattr("carrel.sources.openalex_client.Works", lambda: _Works())

    r = client.get(f"/papers/{pid}/citations?sort=year_asc&offset=1&limit=3")
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "openalex"
    assert len(d["citing"]) == 3
    assert d["next_offset"] == 4
    assert d["citing"][0]["title"].startswith("Live p")


def test_refresh_records_failure(client, session, monkeypatch):
    pid = _seed_paper(session)

    def boom(sess, cfg, paper_id, **kw):
        raise RuntimeError("S2 down")

    monkeypatch.setattr("carrel.api.citations.enrich_paper", boom)

    r = client.post(f"/papers/{pid}/refresh-citations", json={"background": False})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert "S2 down" in r.json()["message"]

    jobs = session.query(Job).all()
    assert any(j.kind == "citations" for j in jobs)


def test_list_citations_resolves_openalex_id(client, session):
    """A citing paper present only in OpenAlex (no DOI / arXiv) must still be
    matched against the library when it carries an OpenAlex id."""
    pid = _seed_paper(session, id="W102", doi=None, arxiv_id=None,
                      citing_papers=[{"title": "From OA", "year": 2024,
                                      "doi": None, "arxiv_id": None,
                                      "s2_paper_id": None,
                                      "openalex_id": "W999"}])
    session.add(Paper(
        id="W999", id_kind="openalex", title="From OA",
        status=PaperStatus.ready.value, oa_status="oa", source="openalex",
        created_at=_now(), updated_at=_now(),
    ))
    session.commit()

    r = client.get(f"/papers/{pid}/citations")
    assert r.status_code == 200
    item = r.json()["citing"][0]
    assert item["openalex_id"] == "W999"
    assert item["in_library"] is True
    assert item["paper_id"] == "W999"


def test_enrich_paper_merges_s2_and_openalex(session, monkeypatch):
    """enrich_paper must combine S2 + OpenAlex citing lists, dedup, and bump the
    count above what S2 alone reported."""
    from carrel.config import CarrelYAML
    from carrel.pipeline.citations import enrich_paper
    from carrel.sources.semanticscholar_client import CitationResult

    paper = Paper(
        id="W200", id_kind="openalex", title="Subject",
        status=PaperStatus.pending.value, oa_status="oa", source="openalex",
        doi="https://doi.org/10.1000/x", created_at=_now(), updated_at=_now(),
    )
    session.add(paper)
    session.commit()

    s2_result = CitationResult(
        s2_paper_id="s2-x",
        citation_count=10,
        influential_count=2,
        reference_count=5,
        citing_papers=[
            {"title": "Shared DOI", "year": 2024, "doi": "10.5000/shared",
             "arxiv_id": None, "s2_paper_id": "s2-a"},
            {"title": "S2 only", "year": 2023, "doi": "10.5000/s2only",
             "arxiv_id": None, "s2_paper_id": "s2-b"},
        ],
    )
    oa_works = [
        # Same DOI as one S2 entry — must dedup, not duplicate.
        {"id": "https://openalex.org/W901", "title": "Shared DOI",
         "publication_year": 2024, "doi": "https://doi.org/10.5000/shared"},
        # OA-only entry — should be added.
        {"id": "https://openalex.org/W902", "title": "OA only",
         "publication_year": 2025, "doi": "https://doi.org/10.5000/oaonly"},
    ]

    monkeypatch.setattr("carrel.pipeline.citations.s2.fetch_citations",
                        lambda **_kw: s2_result)
    monkeypatch.setattr("carrel.pipeline.citations.oa.fetch_citing_works",
                        lambda *_a, **_kw: oa_works)

    ok = enrich_paper(session, CarrelYAML(), "W200")
    assert ok is True

    session.refresh(paper)
    # max(10, len(oa_works)=2) = 10
    assert paper.citation_count == 10
    assert paper.citing_papers is not None
    titles = [c["title"] for c in paper.citing_papers]
    assert titles.count("Shared DOI") == 1, "DOI overlap must dedup"
    assert "S2 only" in titles
    assert "OA only" in titles
    assert len(titles) == 3
    # The merged list contains the OA entry with its openalex_id.
    oa_entry = next(c for c in paper.citing_papers if c["title"] == "OA only")
    assert oa_entry["openalex_id"] == "W902"


def test_enrich_paper_openalex_only_when_s2_misses(session, monkeypatch):
    """When S2 returns None but OpenAlex has hits, save OA data and use its
    list length as the count."""
    from carrel.config import CarrelYAML
    from carrel.pipeline.citations import enrich_paper

    paper = Paper(
        id="W201", id_kind="openalex", title="Subject",
        status=PaperStatus.pending.value, oa_status="oa", source="openalex",
        doi="https://doi.org/10.1000/y", created_at=_now(), updated_at=_now(),
    )
    session.add(paper)
    session.commit()

    oa_works = [
        {"id": "W300", "title": "OA cite", "publication_year": 2024,
         "doi": "https://doi.org/10.5000/just-oa"},
    ]

    monkeypatch.setattr("carrel.pipeline.citations.s2.fetch_citations",
                        lambda **_kw: None)
    monkeypatch.setattr("carrel.pipeline.citations.oa.fetch_citing_works",
                        lambda *_a, **_kw: oa_works)

    ok = enrich_paper(session, CarrelYAML(), "W201")
    assert ok is True

    session.refresh(paper)
    assert paper.citation_count == 1
    assert paper.citing_papers and paper.citing_papers[0]["title"] == "OA cite"
