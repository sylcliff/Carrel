"""Tests for the Semantic Scholar citation client."""
from __future__ import annotations

import httpx
import pytest
from carrel.sources import semanticscholar_client as s2
from carrel.sources.semanticscholar_client import S2Error, fetch_citations, search_papers

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


def test_null_data_array_does_not_crash():
    # S2 sometimes returns {"data": null} (key present, value null) for the
    # citations/references endpoints. Must yield [] rather than TypeError.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/citations"):
            return httpx.Response(200, json={"data": None})
        if request.url.path.endswith("/references"):
            return httpx.Response(200, json={"data": None})
        return httpx.Response(200, json=COUNTS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = fetch_citations(doi="10.1000/xyz", client=client)
    assert res is not None
    assert res.citing_papers == []
    assert res.referenced_papers == []
    assert res.reference_count == 15


# ---------------------------------------------------------------------------
# search_papers
# ---------------------------------------------------------------------------


SEARCH_BODY = {
    "data": [
        {
            "paperId": "s2abc",
            "title": " First RAG Paper ",
            "abstract": "We do RAG.",
            "year": 2024,
            "venue": "NeurIPS",
            "publicationVenue": {"name": "NeurIPS", "type": "conference"},
            "publicationTypes": ["JournalArticle", "Conference"],
            "publicationDate": "2024-05-01",
            "externalIds": {"DOI": "10.1000/a", "ArXiv": "2401.00001v2"},
            "url": "https://semanticscholar.org/paper/s2abc",
            "openAccessPdf": {"url": "https://arxiv.org/pdf/2401.00001"},
            "authors": [{"name": "Alice"}, {"name": "Bob"}],
            "citationCount": 42,
            "referenceCount": 10,
            "fieldsOfStudy": ["Computer Science"],
            "tldr": {"text": "Short and sweet."},
        },
        {
            "paperId": "s2def",
            "title": "Second",
            "abstract": None,
            "year": 2023,
            "venue": None,
            "publicationVenue": None,
            "externalIds": {},
            "authors": [],
            "citationCount": 5,
        },
    ],
}


def _make_search_client(status=200, body=None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, json=body if body is not None else SEARCH_BODY)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_search_relevance_normalizes_rows():
    client, calls = _make_search_client()
    out = search_papers("rag", client=client, limit=5)
    assert len(out) == 2
    first = out[0]
    assert first["s2_paper_id"] == "s2abc"
    assert first["title"] == "First RAG Paper"
    assert first["doi"] == "10.1000/a"
    assert first["arxiv_id"] == "2401.00001"  # version stripped
    assert first["venue"] == "NeurIPS"
    assert first["venue_type"] == "conference"
    assert first["citation_count"] == 42
    assert first["tldr"] == "Short and sweet."
    assert first["authors"] == ["Alice", "Bob"]
    assert first["pdf_url"] == "https://arxiv.org/pdf/2401.00001"
    assert first["publication_date"] == "2024-05-01"
    # Second row: missing fields don't crash.
    assert out[1]["s2_paper_id"] == "s2def"
    assert out[1]["publication_date"] == "2023"  # falls back to year
    assert "/paper/search?" in calls[0]
    assert "/bulk" not in calls[0]


def test_search_citations_uses_bulk_endpoint():
    client, calls = _make_search_client()
    search_papers("rag", client=client, sort="citations", limit=10)
    assert "/paper/search/bulk?" in calls[0]
    assert "sort=citationCount%3Adesc" in calls[0]


def test_bulk_endpoint_does_not_request_tldr():
    # The bulk endpoint rejects `tldr` with HTTP 400; it must only appear on
    # the relevance endpoint's field list.
    client, calls = _make_search_client()
    search_papers("rag", client=client, sort="citations", limit=10)
    bulk_url = calls[0]
    # Parse fields= out of the query string and assert tldr is absent.
    from urllib.parse import urlparse, parse_qs
    fields = parse_qs(urlparse(bulk_url).query).get("fields", [""])[0]
    assert "tldr" not in fields.split(",")

    client2, calls2 = _make_search_client()
    search_papers("rag", client=client2, sort="relevance", limit=10)
    rel_fields = parse_qs(urlparse(calls2[0]).query).get("fields", [""])[0]
    assert "tldr" in rel_fields.split(",")


def test_search_propagates_year_and_filters_to_query():
    client, calls = _make_search_client()
    search_papers(
        "rag", client=client,
        year_from=2022, year_to=2024, min_citations=10,
        fields_of_study=["Computer Science"], open_access_only=True,
    )
    url = calls[0]
    assert "year=2022-2024" in url
    assert "minCitationCount=10" in url
    assert "fieldsOfStudy=Computer+Science" in url or "fieldsOfStudy=Computer%20Science" in url
    assert "openAccessPdf" in url


def test_search_min_citations_post_filters_when_bulk(monkeypatch):
    # The relevance endpoint supports minCitationCount server-side; test that
    # rows below the threshold are dropped as a safety net regardless.
    body = {"data": [
        {"paperId": "low", "title": "Low", "citationCount": 1, "year": 2024, "externalIds": {}},
        {"paperId": "high", "title": "High", "citationCount": 100, "year": 2024, "externalIds": {}},
    ]}
    client, _ = _make_search_client(body=body)
    out = search_papers("x", client=client, limit=10, min_citations=50)
    assert [r["s2_paper_id"] for r in out] == ["high"]


def test_search_429_raises_s2error(monkeypatch):
    monkeypatch.setattr(s2.time, "sleep", lambda *_a, **_k: None)
    client, _ = _make_search_client(status=429, body={"error": "rate"})
    with pytest.raises(S2Error):
        search_papers("x", client=client)


def test_search_empty_query_returns_empty():
    client, _ = _make_search_client()
    assert search_papers("  ", client=client) == []
