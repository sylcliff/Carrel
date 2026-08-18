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
