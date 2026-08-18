"""Tests for the Semantic Scholar citation client."""
from __future__ import annotations

import httpx
import pytest

from carrel.sources import semanticscholar_client as s2
from carrel.sources.semanticscholar_client import S2Error, fetch_citations

COUNTS = {
    "paperId": "abc123paperid",
    "externalIds": {"DOI": "10.1000/xyz", "ArXiv": "2301.12345v2"},
    "citationCount": 42,
    "influentialCitationCount": 3,
    "referenceCount": 15,
}

CITATIONS = {
    "data": [
        {"citingPaper": {
            "paperId": "cite1",
            "title": " First Citing Paper ",
            "year": 2024,
            "externalIds": {"DOI": "10.2000/a", "ArXiv": "2401.00001"},
        }},
        {"citingPaper": {
            "paperId": "cite2",
            "title": "Second",
            "year": 2023,
            "externalIds": {"DOI": "https://doi.org/10.2000/b"},
        }},
        {"citingPaper": {
            "paperId": "cite3",
            "title": None,
            "year": None,
            "externalIds": {},
        }},
    ],
}


def _make_client(counts_status=200, counts_body=None, cite_status=200, cite_body=None):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/citations"):
            return httpx.Response(cite_status, json=cite_body if cite_body is not None else CITATIONS)
        return httpx.Response(counts_status, json=counts_body if counts_body is not None else COUNTS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, calls


def test_fetch_by_doi_priority_and_normalization():
    client, calls = _make_client()
    res = fetch_citations(doi="10.1000/xyz", arxiv_id="2301.12345", client=client)
    assert res is not None
    assert res.s2_paper_id == "abc123paperid"
    assert res.citation_count == 42
    assert res.influential_count == 3
    assert res.reference_count == 15
    # First call used the DOI form
    assert "/paper/DOI:10.1000/xyz" in calls[0][1] or calls[0][1].endswith("/paper/DOI:10.1000/xyz")
    # Citing list normalized: title stripped, DOI cleaned, arXiv version stripped
    first = res.citing_papers[0]
    assert first["title"] == "First Citing Paper"
    assert first["arxiv_id"] == "2401.00001"
    assert res.citing_papers[1]["doi"] == "10.2000/b"
    assert res.citing_papers[2]["title"] is None
    assert len(res.citing_papers) == 3


def test_fetch_by_arxiv_when_no_doi():
    client, calls = _make_client()
    fetch_citations(arxiv_id="2301.12345", client=client)
    assert "/paper/ARXIV:2301.12345" in calls[0][1] or calls[0][1].endswith("/paper/ARXIV:2301.12345")


def test_fetch_uses_explicit_s2_id_first():
    client, calls = _make_client()
    fetch_citations(s2_id="abc123paperid", doi="10.1000/xyz", client=client)
    assert calls[0][1].endswith("/paper/abc123paperid")


def test_no_identifier_returns_none():
    client, _ = _make_client()
    assert fetch_citations(client=client) is None


def test_404_counts_returns_none():
    client, _ = _make_client(counts_status=404, counts_body={"error": "not found"})
    assert fetch_citations(doi="10.1/missing", client=client) is None


def test_429_retries_then_raises(monkeypatch):
    # Avoid real sleeps.
    monkeypatch.setattr(s2.time, "sleep", lambda *_a, **_k: None)
    client, _ = _make_client(counts_status=429, counts_body={"error": "rate limit"})
    with pytest.raises(S2Error):
        fetch_citations(doi="10.1000/xyz", client=client)


def test_404_on_citations_yields_empty_list():
    client, _ = _make_client(cite_status=404, cite_body={"error": "nf"})
    res = fetch_citations(doi="10.1000/xyz", client=client)
    assert res is not None
    assert res.citing_papers == []
    assert res.citation_count == 42
