"""Tests for normalize.py (source shape -> PaperRecord)."""
from __future__ import annotations

from unittest.mock import patch

from carrel.sources.arxiv import ArxivEntry
from carrel.sources.normalize import (
    enrich_with_openalex,
    from_arxiv,
    from_openalex,
)


def _arxiv_entry(**over) -> ArxivEntry:
    base = dict(
        arxiv_id="2401.00001v2",
        title="An arXiv Paper",
        summary="  abstract with\nnewline  ",
        authors=["Alice", "Bob"],
        categories=["cs.CL"],
        updated="2026-08-01T12:00:00Z",
        abs_url="https://arxiv.org/abs/2401.00001v2",
        pdf_url="https://arxiv.org/pdf/2401.00001v2",
    )
    base.update(over)
    return ArxivEntry(**base)


def _oa_work(**over) -> dict:
    base = {
        "id": "https://openalex.org/W100",
        "title": "An OA Paper",
        "doi": "https://doi.org/10.48550/arXiv.2401.00001",
        "publication_date": "2026-08-01",
        "abstract_inverted_index": {"hello": [0], "world": [1]},
        "open_access": {"is_oa": True},
        "best_oa_location": {
            "pdf_url": "https://arxiv.org/pdf/2401.00001.pdf",
            "landing_page_url": "https://arxiv.org/abs/2401.00001",
        },
        "locations": [],
        "primary_location": {"source": {"display_name": "arXiv"}},
        "authorships": [
            {"author": {"id": "https://openalex.org/A1", "display_name": "Alice X"},
             "institutions": [{"display_name": "CMU"}]}
        ],
    }
    base.update(over)
    return base


def test_from_arxiv_strips_version_and_normalizes():
    rec = from_arxiv(_arxiv_entry())
    assert rec.id == "arxiv:2401.00001"
    assert rec.arxiv_id == "2401.00001"
    assert rec.id_kind == "arxiv"
    assert rec.title == "An arXiv Paper"
    # from_arxiv preserves the summary string; the Atom parser collapses
    # whitespace at parse time, not here.
    assert rec.abstract == "  abstract with\nnewline  "
    assert rec.oa_status == "oa"
    assert rec.source == "arxiv"
    assert rec.authors[0]["name"] == "Alice"


def test_from_openalex_uses_work_id_and_strips_arxiv_version():
    rec = from_openalex(_oa_work())
    assert rec.id == "W100"
    assert rec.id_kind == "openalex"
    assert rec.arxiv_id == "2401.00001"  # version stripped
    assert rec.abstract == "hello world"
    assert rec.source == "openalex"


def test_from_openalex_arxiv_fallback_when_no_work_id():
    work = _oa_work(id=None, doi="https://doi.org/10.48550/arXiv.2401.00001")
    rec = from_openalex(work)
    assert rec.id == "arxiv:2401.00001"
    assert rec.id_kind == "arxiv"


def test_from_openalex_empty_id_when_nothing_identifiable():
    work = _oa_work(
        id=None,
        doi=None,
        best_oa_location={"pdf_url": None, "landing_page_url": "https://x"},
    )
    rec = from_openalex(work)
    assert rec.id == ""
    assert rec.id_kind == "openalex"  # source was OpenAlex even without an ID


def test_enrich_with_openalex_promotes_identity():
    arxiv_rec = from_arxiv(_arxiv_entry())
    with patch(
        "carrel.sources.normalize.oa.lookup_by_arxiv_id",
        return_value=_oa_work(),
    ):
        enriched = enrich_with_openalex(arxiv_rec)
    assert enriched.id == "W100"
    assert enriched.id_kind == "openalex"
    assert enriched.source == "both"
    assert enriched.arxiv_id == "2401.00001"
    assert enriched.authors[0]["openalex_author_id"] == "A1"
    assert enriched.abstract == "hello world"


def test_enrich_keeps_arxiv_when_openalex_missing():
    arxiv_rec = from_arxiv(_arxiv_entry())
    with patch("carrel.sources.normalize.oa.lookup_by_arxiv_id", return_value=None):
        enriched = enrich_with_openalex(arxiv_rec)
    assert enriched.id == "arxiv:2401.00001"
    assert enriched.id_kind == "arxiv"
    assert enriched.source == "arxiv"


def test_enrich_keeps_arxiv_pdf_when_oa_has_none():
    arxiv_rec = from_arxiv(_arxiv_entry())
    oa_work = _oa_work(
        best_oa_location={"pdf_url": None, "landing_page_url": "https://x"},
        open_access={"is_oa": False},
    )
    with patch("carrel.sources.normalize.oa.lookup_by_arxiv_id", return_value=oa_work):
        enriched = enrich_with_openalex(arxiv_rec)
    # OA PDF wins when present; here OA has none so arXiv PDF is retained.
    assert enriched.pdf_url == "https://arxiv.org/pdf/2401.00001v2"
    assert enriched.oa_status == "oa"


def test_enrich_noop_when_not_arxiv_record():
    rec = from_openalex(_oa_work())
    with patch("carrel.sources.normalize.oa.lookup_by_arxiv_id") as p:
        out = enrich_with_openalex(rec)
        p.assert_not_called()
    assert out is rec


def test_paperrecord_is_slotted():
    # Sanity: slotted dataclass rejects unknown attrs (catches typos).
    import pytest
    rec = from_arxiv(_arxiv_entry())
    with pytest.raises(AttributeError):
        rec.nonexistent_field = 1  # type: ignore[misc]
