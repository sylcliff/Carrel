"""Tests for arXiv→journal publication detection and candidate selection."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from carrel.config import CarrelYAML
from carrel.models import Paper, PaperStatus
from carrel.pipeline import publication_check as pc


NOW = datetime(2026, 8, 22, tzinfo=UTC)
TODAY = NOW.date()


def _make_paper(session, **kw) -> Paper:
    base = dict(
        id="W1",
        id_kind="arxiv",
        title="An arXiv paper",
        status=PaperStatus.parsed.value,
        oa_status="oa",
        source="arxiv",
        arxiv_id="2301.01234",
        in_library=True,
        discarded=False,
        created_at=NOW - timedelta(days=300),
        updated_at=NOW,
    )
    base.update(kw)
    p = Paper(**base)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# age helpers
# ---------------------------------------------------------------------------


def test_is_old_enough():
    assert pc._is_old_enough("2020-01-01T00:00:00Z", min_age_days=180, now=TODAY) is True
    assert pc._is_old_enough((TODAY - timedelta(days=10)).isoformat(), min_age_days=180, now=TODAY) is False
    # Unknown publication date → eligible (cheap to check).
    assert pc._is_old_enough(None, min_age_days=180, now=TODAY) is True


# ---------------------------------------------------------------------------
# detection: S2 / OpenAlex signals
# ---------------------------------------------------------------------------


def _stub_arxiv(monkeypatch, published: str | None):
    from carrel.sources import arxiv as arxiv_source

    monkeypatch.setattr(
        arxiv_source, "fetch_one",
        lambda _id: SimpleNamespace(published=published) if published else None,
    )


def _stub_sources(monkeypatch, *, s2_row=None, oa_work=None):
    from carrel.sources import openalex_client as oa_mod
    from carrel.sources import semanticscholar_client as s2_mod

    monkeypatch.setattr(s2_mod, "fetch_paper", lambda _id: s2_row)
    monkeypatch.setattr(oa_mod, "lookup_by_arxiv_id", lambda _id, title_hint=None: oa_work)


def test_detect_too_young_short_circuits(session, monkeypatch):
    p = _make_paper(session)
    _stub_arxiv(monkeypatch, (TODAY - timedelta(days=10)).isoformat())
    called = {"s2": False}
    from carrel.sources import semanticscholar_client as s2_mod

    def _boom(_id):  # pragma: no cover - must not be called
        called["s2"] = True
        return None

    monkeypatch.setattr(s2_mod, "fetch_paper", _boom)
    info = pc.detect_publication(p, min_age_days=180, now=TODAY)
    assert info.found is False
    assert info.reason == "too young"
    assert called["s2"] is False


def test_detect_s2_journal_hit(session, monkeypatch):
    p = _make_paper(session)
    _stub_arxiv(monkeypatch, "2020-01-01T00:00:00Z")
    _stub_sources(
        monkeypatch,
        s2_row={
            "doi": "https://doi.org/10.1021/acs.jctc.6c01122",
            "venue": "Journal of Chemical Theory and Computation",
            "venue_type": "journal",
            "publication_date": "2021-06-01",
        },
    )
    info = pc.detect_publication(p, min_age_days=180, now=TODAY)
    assert info.found is True
    assert info.journal_doi == "10.1021/acs.jctc.6c01122"
    assert info.source == "semanticscholar"


def test_detect_arxiv_doi_not_a_journal(session, monkeypatch):
    p = _make_paper(session)
    _stub_arxiv(monkeypatch, "2020-01-01T00:00:00Z")
    _stub_sources(
        monkeypatch,
        s2_row={
            "doi": "10.48550/arXiv.2301.01234",  # arXiv's own DOI — not a journal
            "venue": "arXiv",
            "venue_type": "repository",
            "publication_date": "2023-01-01",
        },
        oa_work=None,
    )
    info = pc.detect_publication(p, min_age_days=180, now=TODAY)
    assert info.found is False
    assert info.reason == "no journal DOI found"


def test_detect_openalex_fallback_when_s2_is_arxiv(session, monkeypatch):
    p = _make_paper(session)
    _stub_arxiv(monkeypatch, "2020-01-01T00:00:00Z")
    # S2 only knows the arXiv DOI; OpenAlex knows the published journal version.
    _stub_sources(
        monkeypatch,
        s2_row={
            "doi": "10.48550/arXiv.2301.01234",
            "venue": "arXiv",
            "venue_type": "repository",
            "publication_date": None,
        },
        oa_work={
            "doi": "https://doi.org/10.1103/PhysRevX.10.011001",
            "primary_location": {"source": {"type": "journal", "display_name": "Phys Rev X"}},
            "publication_date": "2020-03-01",
        },
    )
    info = pc.detect_publication(p, min_age_days=180, now=TODAY)
    assert info.found is True
    assert info.journal_doi == "10.1103/PhysRevX.10.011001"
    assert info.source == "openalex"


# ---------------------------------------------------------------------------
# candidate selection
# ---------------------------------------------------------------------------


def test_select_candidates_filters(session):
    # Eligible: old, in library, arxiv id, no journal_doi, never checked.
    eligible = _make_paper(session, id="W-elig", created_at=NOW - timedelta(days=400))
    # Too young (created recently) → excluded.
    _make_paper(session, id="W-young", created_at=NOW - timedelta(days=10))
    # Not in library → excluded.
    _make_paper(session, id="W-inbox", in_library=False, created_at=NOW - timedelta(days=400))
    # Already has journal_doi → excluded.
    _make_paper(session, id="W-done", journal_doi="10.1/x", created_at=NOW - timedelta(days=400))
    # Recently checked (within throttle window) → excluded.
    _make_paper(
        session, id="W-checked",
        created_at=NOW - timedelta(days=400),
        published_checked_at=NOW - timedelta(days=5),
    )

    rows = pc.select_candidates(
        session, limit=50, now=NOW, min_age_days=180, throttle_days=30
    )
    ids = {r.id for r in rows}
    assert ids == {"W-elig"}


def test_select_candidates_never_checked_first(session):
    a = _make_paper(session, id="W-old-checked",
                    created_at=NOW - timedelta(days=500),
                    published_checked_at=NOW - timedelta(days=60))
    b = _make_paper(session, id="W-newer-unchecked",
                    created_at=NOW - timedelta(days=200))
    rows = pc.select_candidates(
        session, limit=50, now=NOW, min_age_days=180, throttle_days=30
    )
    assert [r.id for r in rows] == ["W-newer-unchecked", "W-old-checked"]


# ---------------------------------------------------------------------------
# check_and_apply: end-to-end with remote + process stubbed
# ---------------------------------------------------------------------------


def test_check_and_apply_records_journal_doi_even_when_pdf_fetch_fails(
    session, cfg: CarrelYAML, tmp_path, monkeypatch
):
    from carrel.sources import remote_downloader as rd_mod

    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session)

    _stub_arxiv(monkeypatch, "2020-01-01T00:00:00Z")
    _stub_sources(
        monkeypatch,
        s2_row={
            "doi": "10.1021/acs.jctc.6c01122",
            "venue": "JCTC",
            "venue_type": "journal",
            "publication_date": "2021-06-01",
        },
    )
    # Remote is "configured" but the PDF fetch fails — arXiv version must stay.
    monkeypatch.setattr(rd_mod, "is_configured", lambda: True)
    monkeypatch.setattr(
        rd_mod, "download_paper",
        lambda *a, **k: (_ for _ in ()).throw(rd_mod.RemoteTransientError("boom")),
    )

    out = pc.check_and_apply(session, cfg, p.id, force=True)

    # Detection was recorded even though the PDF couldn't be fetched.
    assert out.journal_doi == "10.1021/acs.jctc.6c01122"
    assert out.published_checked_at is not None
    assert "PDF fetch failed" in (out.error or "")
    # Nothing was swapped.
    assert out.pdf_origin != "journal"
    assert out.pdf_files is None


def test_check_and_apply_happy_path_swaps_and_reprocesses(
    session, cfg: CarrelYAML, tmp_path, monkeypatch
):
    from carrel.sources import remote_downloader as rd_mod

    cfg.storage.root = tmp_path / "data"
    # Start with an existing arXiv PDF on disk (the active paper.pdf).
    p = _make_paper(session, status=PaperStatus.parsed.value, md_path=None)
    work_dir, pdf_dest, _md, rel_prefix = __import__(
        "carrel.pipeline.process", fromlist=["paper_paths"]
    ).paper_paths(p, cfg)
    pdf_dest.parent.mkdir(parents=True, exist_ok=True)
    pdf_dest.write_bytes(b"%PDF-1.7\narxiv version")
    session.add(p)
    session.commit()

    _stub_arxiv(monkeypatch, "2020-01-01T00:00:00Z")
    _stub_sources(
        monkeypatch,
        s2_row={
            "doi": "10.1021/acs.jctc.6c01122",
            "venue": "JCTC",
            "venue_type": "journal",
            "publication_date": "2021-06-01",
        },
    )

    def _fake_download(identifier, dest_dir, *, filename="paper.pdf", env=None):
        dest = work_dir / filename
        dest.write_bytes(b"%PDF-1.7\njournal version bytes")
        return dest

    monkeypatch.setattr(rd_mod, "is_configured", lambda: True)
    monkeypatch.setattr(rd_mod, "download_paper", _fake_download)

    # process_paper would normally re-run MinerU; stub it. The download step is
    # a no-op because paper.pdf already exists on disk.
    reprocessed = {"n": 0}

    def _fake_process(session, cfg, paper_id, **kw):
        reprocessed["n"] += 1
        paper = session.get(Paper, paper_id)
        paper.status = PaperStatus.parsed.value
        paper.md_path = f"{rel_prefix}/paper.md"
        session.add(paper)
        session.commit()
        return paper

    monkeypatch.setattr(pc, "process_paper", _fake_process)

    out = pc.check_and_apply(session, cfg, p.id, force=True)

    # Both variants recorded, journal is active.
    assert out.journal_doi == "10.1021/acs.jctc.6c01122"
    assert out.pdf_origin == "journal"
    assert out.oa_status == "institutional"
    assert out.pdf_files and out.pdf_files["arxiv"].endswith("arxiv.pdf")
    assert out.pdf_files["journal"].endswith("journal.pdf")
    # Both files on disk; active paper.pdf is the journal version.
    assert (work_dir / "arxiv.pdf").read_bytes() == b"%PDF-1.7\narxiv version"
    assert (work_dir / "journal.pdf").read_bytes() == b"%PDF-1.7\njournal version bytes"
    assert pdf_dest.read_bytes() == b"%PDF-1.7\njournal version bytes"
    assert reprocessed["n"] == 1


def test_check_and_apply_idempotent_when_already_checked(
    session, cfg: CarrelYAML, tmp_path, monkeypatch
):
    cfg.storage.root = tmp_path / "data"
    p = _make_paper(session, journal_doi="10.1/x")
    called = {"n": 0}
    from carrel.sources import arxiv as arxiv_source

    def _boom(_id):  # pragma: no cover - must not be called
        called["n"] += 1
        return None

    monkeypatch.setattr(arxiv_source, "fetch_one", _boom)
    out = pc.check_and_apply(session, cfg, p.id, force=False)
    assert out.journal_doi == "10.1/x"
    assert called["n"] == 0
