"""Tests for openalex_client field helpers (no network)."""
from __future__ import annotations

from carrel.sources import openalex_client as oa


def _work(**over):
    work = {
        "id": "https://openalex.org/W123",
        "title": "A Study of Things",
        "doi": "https://doi.org/10.1234/abc",
        "publication_date": "2026-08-01",
        "abstract_inverted_index": {"large": [0], "models": [1], "rock": [2]},
        "open_access": {"is_oa": True},
        "best_oa_location": {
            "pdf_url": "https://arxiv.org/pdf/2401.00001.pdf",
            "landing_page_url": "https://arxiv.org/abs/2401.00001",
        },
        "locations": [],
        "primary_location": {"source": {"display_name": "arXiv"}},
        "authorships": [
            {
                "author": {
                    "id": "https://openalex.org/A999",
                    "display_name": "Jane Doe",
                },
                "institutions": [{"display_name": "MIT"}],
            }
        ],
    }
    work.update(over)
    return work


def test_work_id_strips_prefix():
    assert oa.work_id(_work()) == "W123"
    assert oa.work_id({"id": "W123"}) == "W123"
    assert oa.work_id({}) == ""


def test_work_abstract_restores_inverted_index():
    assert oa.work_abstract(_work()) == "large models rock"
    assert oa.work_abstract(None) is None
    # no inverted index, no flat abstract -> None (not "")
    assert oa.work_abstract({"abstract_inverted_index": None, "abstract": ""}) is None


def test_work_abstract_prefers_flat_when_present():
    w = _work(abstract_inverted_index=None, abstract="flat abstract text")
    assert oa.work_abstract(w) == "flat abstract text"


def test_work_arxiv_id_from_doi():
    w = _work(doi="https://doi.org/10.48550/arXiv.2401.00001")
    assert oa.work_arxiv_id(w) == "2401.00001"


def test_work_arxiv_id_from_ids_field():
    w = _work(doi=None, ids={"arxiv": "https://arxiv.org/abs/2401.00002"})
    assert oa.work_arxiv_id(w) == "2401.00002"


def test_work_arxiv_id_none_for_non_arxiv():
    assert oa.work_arxiv_id(_work(doi="https://doi.org/10.1234/other")) is None


def test_work_pdf_url_direct_pdf_only():
    # OA with a direct pdf_url
    url, status = oa.work_pdf_url(_work())
    assert url == "https://arxiv.org/pdf/2401.00001.pdf"
    assert status == "oa"


def test_work_pdf_url_rejects_landing_page_only():
    # OA but HTML-only: must not return a landing page as a "pdf".
    w = _work(best_oa_location={
        "pdf_url": None,
        "landing_page_url": "https://publisher.example/abc",
    })
    url, status = oa.work_pdf_url(w)
    assert url is None
    assert status == "none"


def test_work_pdf_url_searches_locations():
    w = _work(
        best_oa_location={"pdf_url": None, "landing_page_url": "https://x"},
        locations=[{"pdf_url": "https://other.example/paper.pdf"}],
    )
    url, status = oa.work_pdf_url(w)
    assert url == "https://other.example/paper.pdf"
    assert status == "oa"


def test_work_pdf_url_closed():
    w = _work(open_access={"is_oa": False})
    url, status = oa.work_pdf_url(w)
    assert url is None
    assert status == "closed"


def test_work_authors_shape():
    authors = oa.work_authors(_work())
    assert authors == [{
        "name": "Jane Doe",
        "openalex_author_id": "A999",
        "affiliation": "MIT",
    }]


def test_work_venue():
    assert oa.work_venue(_work()) == "arXiv"
    assert oa.work_venue({"primary_location": None}) is None
