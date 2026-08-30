"""Tests for the Crossref REST API client + field helpers."""
from __future__ import annotations

import httpx
import pytest

from carrel.sources import crossref_client as cr
from carrel.sources.throttle import crossref_throttle


# ---------------------------------------------------------------------------
# Field helpers (no HTTP)
# ---------------------------------------------------------------------------


def _full_row(**overrides):
    row = {
        "DOI": "10.1234/example.2023",
        "title": ["Example Title"],
        "author": [
            {
                "given": "Ada",
                "family": "Lovelace",
                "ORCID": "https://orcid.org/0000-0002-1825-0097",
                "affiliation": [{"name": "University of London"}],
            },
            {
                "given": "Charles",
                "family": "Babbage",
            },
        ],
        "container-title": ["Nature Machine Intelligence"],
        "type": "journal-article",
        "abstract": "<jats:p>We present the <jats:bold>Analytical Engine</jats:bold>.</jats:p>",
        "published-print": {"date-parts": [[2023, 5, 12]]},
        "issued": {"date-parts": [[2023, 4, 1]]},
        "link": [
            {"URL": "https://example.com/landing", "content-version": "vor",
             "content-type": "text/html"},
            {"URL": "https://example.com/paper.pdf", "content-version": "vor",
             "content-type": "application/pdf"},
        ],
    }
    row.update(overrides)
    return row


def test_work_doi_returns_bare_doi():
    assert cr.work_doi(_full_row()) == "10.1234/example.2023"
    assert cr.work_doi(None) is None
    assert cr.work_doi({}) is None


def test_work_title_takes_first():
    assert cr.work_title(_full_row()) == "Example Title"
    assert cr.work_title({"title": ["  Spaced  ", "Alt"]}) == "Spaced"
    assert cr.work_title({}) == "(untitled)"
    assert cr.work_title(None) == "(untitled)"


def test_work_authors_join_given_family_and_strip_orcid():
    authors = cr.work_authors(_full_row())
    assert len(authors) == 2
    assert authors[0]["name"] == "Ada Lovelace"
    assert authors[0]["orcid"] == "0000-0002-1825-0097"
    assert authors[0]["affiliation"] == "University of London"
    assert authors[1]["name"] == "Charles Babbage"
    assert authors[1]["orcid"] is None
    assert authors[1]["affiliation"] is None


def test_work_authors_skips_empty_names():
    row = _full_row(author=[{"given": "", "family": ""}, {"name": "Fallback Name"}])
    authors = cr.work_authors(row)
    assert [a["name"] for a in authors] == ["Fallback Name"]


def test_work_venue_takes_first_container_title():
    assert cr.work_venue(_full_row()) == "Nature Machine Intelligence"
    assert cr.work_venue({"container-title": []}) is None
    assert cr.work_venue(None) is None


def test_work_venue_type_maps_known_types():
    assert cr.work_venue_type({"type": "journal-article"}) == "journal"
    assert cr.work_venue_type({"type": "proceedings-article"}) == "conference"
    assert cr.work_venue_type({"type": "book-chapter"}) == "book"
    assert cr.work_venue_type({"type": "book-part"}) == "book"
    assert cr.work_venue_type({"type": "book"}) == "book"
    assert cr.work_venue_type({"type": "posted-content"}) == "repository"
    # Unknown types pass through raw.
    assert cr.work_venue_type({"type": "report"}) == "report"
    assert cr.work_venue_type({}) is None
    assert cr.work_venue_type(None) is None


def test_work_abstract_strips_jats_tags_and_collapses_whitespace():
    row = {"abstract": "<jats:p>Line1.</jats:p>  <jats:p>Line2.</jats:p>"}
    assert cr.work_abstract(row) == "Line1. Line2."
    assert cr.work_abstract({}) is None
    assert cr.work_abstract(None) is None
    # Empty after strip.
    assert cr.work_abstract({"abstract": "<jats:p>   </jats:p>"}) is None


def test_work_publication_date_prefers_published_print_and_formats():
    # Year-month-day when all three are present.
    assert cr.work_publication_date(_full_row()) == "2023-05-12"
    # Year-month when day is missing.
    row = {"published-print": {"date-parts": [[2023, 6]]}}
    assert cr.work_publication_date(row) == "2023-06"
    # Year only when month/day missing.
    row = {"published-print": {"date-parts": [[2019]]}}
    assert cr.work_publication_date(row) == "2019"


def test_work_publication_date_falls_through_blocks_in_priority():
    # Missing published-print → use issued.
    row = {"issued": {"date-parts": [[2020, 3, 1]]}}
    assert cr.work_publication_date(row) == "2020-03-01"
    # Only created available.
    row = {"created": {"date-parts": [[2018]]}}
    assert cr.work_publication_date(row) == "2018"
    # Last-ditch: issued.year only.
    row = {"issued": {"year": "2017"}}
    assert cr.work_publication_date(row) == "2017"
    # No date anywhere.
    assert cr.work_publication_date({}) is None


def test_work_pdf_url_picks_vor_or_am_pdf():
    # vor + .pdf wins.
    assert cr.work_pdf_url(_full_row()) == "https://example.com/paper.pdf"
    # arxiv.org/pdf is accepted even without .pdf suffix in url.
    row = {"link": [{"URL": "https://arxiv.org/pdf/2401.00001v2",
                      "content-version": "am"}]}
    assert cr.work_pdf_url(row) == "https://arxiv.org/pdf/2401.00001v2"
    # Rejects content-version=unspecified.
    row = {"link": [{"URL": "https://example.com/paper.pdf",
                      "content-version": "unspecified"}]}
    assert cr.work_pdf_url(row) is None
    # Rejects HTML even with .pdf in URL.
    row = {"link": [{"URL": "https://example.com/landing",
                      "content-version": "vor"}]}
    assert cr.work_pdf_url(row) is None
    assert cr.work_pdf_url(None) is None
    assert cr.work_pdf_url({}) is None


def test_work_arxiv_id_parses_arxiv_doi():
    row = {"DOI": "10.48550/arXiv.2401.00001v2"}
    assert cr.work_arxiv_id(row) == "2401.00001"
    row = {"DOI": "10.48550/ARXIV.2301.00001"}  # case-insensitive
    assert cr.work_arxiv_id(row) == "2301.00001"
    # Non-arxiv DOI.
    assert cr.work_arxiv_id({"DOI": "10.1234/example"}) is None
    assert cr.work_arxiv_id({"DOI": None}) is None
    assert cr.work_arxiv_id({}) is None
    assert cr.work_arxiv_id(None) is None


# ---------------------------------------------------------------------------
# HTTP / search_papers
# ---------------------------------------------------------------------------


def _make_client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.crossref.test",
        transport=httpx.MockTransport(handler),
    )


def _search_payload(items):
    return {"status": "ok", "message": {"items": items}}


def _dummy_cfg():
    from carrel.config import CrossrefConfig

    return CrossrefConfig(
        base_url="https://api.crossref.test",
        mailto="test@example.com",
        request_timeout_seconds=10,
        max_retries=3,
    )


def test_search_papers_builds_url_and_returns_items():
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url.params))
        return httpx.Response(
            200,
            json=_search_payload([_full_row(), _full_row(DOI="10.5/b")]),
        )

    cr.configure(_dummy_cfg())
    try:
        rows = cr.search_papers("transformer", limit=10, client=_make_client(handler))
    finally:
        cr._client = None  # don't let configure() leak across tests

    assert len(rows) == 2
    assert "query.bibliographic=transformer" in captured[0]
    assert "rows=10" in captured[0]
    # No sort param for relevance.
    assert "sort=" not in captured[0]


def test_search_papers_applies_year_filter():
    captured: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params)
        return httpx.Response(200, json=_search_payload([]))

    cr.configure(_dummy_cfg())
    try:
        cr.search_papers("x", year_from=2020, year_to=2023, client=_make_client(handler))
    finally:
        cr._client = None
    # Two filter params (Crossref repeats filter= for each).
    filters = captured[0].get_list("filter")
    assert "from-pub-date:2020" in filters
    assert "until-pub-date:2023" in filters


def test_search_papers_date_sort_adds_published_desc():
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url.params))
        return httpx.Response(200, json=_search_payload([]))

    cr.configure(_dummy_cfg())
    try:
        cr.search_papers("x", sort="date", client=_make_client(handler))
    finally:
        cr._client = None
    assert "sort=published" in captured[0]
    assert "order=desc" in captured[0]


def test_search_papers_citations_sort_is_silently_ignored():
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url.params))
        return httpx.Response(200, json=_search_payload([]))

    cr.configure(_dummy_cfg())
    try:
        cr.search_papers("x", sort="citations", client=_make_client(handler))
    finally:
        cr._client = None
    # No sort= or order= on the wire.
    assert "sort=" not in captured[0]
    assert "order=" not in captured[0]


def test_search_papers_empty_query_returns_empty():
    assert cr.search_papers("") == []
    assert cr.search_papers("   ") == []


def test_search_papers_404_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    cr.configure(_dummy_cfg())
    try:
        rows = cr.search_papers("x", client=_make_client(handler))
    finally:
        cr._client = None
    assert rows == []


def test_search_papers_429_records_throttle_and_returns_empty(monkeypatch):
    monkeypatch.setattr(cr.time, "sleep", lambda *_a, **_k: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"}, json={"error": "rate"})

    cr.configure(_dummy_cfg())
    try:
        rows = cr.search_papers("x", client=_make_client(handler))
    finally:
        cr._client = None

    # First call: 3 attempts (max_retries=3), each 429. Throttle is now open.
    assert rows == []
    assert crossref_throttle.is_open()
    crossref_throttle.clear()
    assert not crossref_throttle.is_open()


def test_search_papers_5xx_retries_then_raises(monkeypatch):
    monkeypatch.setattr(cr.time, "sleep", lambda *_a, **_k: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    cr.configure(_dummy_cfg())
    try:
        with pytest.raises(cr.CrossrefError):
            cr.search_papers("x", client=_make_client(handler))
    finally:
        cr._client = None


def test_search_papers_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(cr.time, "sleep", lambda *_a, **_k: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    cr.configure(_dummy_cfg())
    try:
        with pytest.raises(cr.CrossrefError):
            cr.search_papers("x", client=_make_client(handler))
    finally:
        cr._client = None


def test_fetch_paper_404_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    cr.configure(_dummy_cfg())
    try:
        result = cr.fetch_paper("10.1234/missing", client=_make_client(handler))
    finally:
        cr._client = None
    assert result is None


def test_fetch_paper_returns_message_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "message": _full_row()})

    cr.configure(_dummy_cfg())
    try:
        result = cr.fetch_paper("10.1234/x", client=_make_client(handler))
    finally:
        cr._client = None
    assert result is not None
    assert result["DOI"] == "10.1234/example.2023"
