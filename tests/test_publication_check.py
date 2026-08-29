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


# ---------------------------------------------------------------------------
# fill_closed_papers: skip / end-of-batch retry
# ---------------------------------------------------------------------------


def _seed_remote_candidate(session, *, id: str, **kw) -> Paper:
    """A paper that ``select_remote_candidates`` will pick up.

    Defaults: in_library, no PDF, pending status, has a DOI. Override any of
    these to test edge cases (e.g. pass ``doi=None, arxiv_id=None, journal_doi=None``
    to get a paper that needs to be *skipped*).
    """
    base = dict(
        id=id,
        id_kind="doi",
        title=f"Paper {id}",
        status=PaperStatus.pending.value,
        oa_status=None,
        doi="10.1/test",
        arxiv_id=None,
        journal_doi=None,
        pdf_url=None,
        pdf_path=None,
        in_library=True,
        discarded=False,
        created_at=NOW - timedelta(days=10),
        updated_at=NOW,
    )
    base.update(kw)
    p = Paper(**base)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def test_fill_closed_papers_returns_reason_when_not_configured(
    session, cfg: CarrelYAML, monkeypatch
):
    from carrel.sources import remote_downloader as rd_mod

    monkeypatch.setattr(rd_mod, "is_configured", lambda: False)
    out = pc.fill_closed_papers(session, cfg, limit=10)
    assert out == {
        "candidates": 0, "parsed": 0, "failed": 0, "skipped": 0,
        "retried": 0, "retried_ok": 0, "reason": "remote not configured",
    }


def test_fill_closed_papers_skips_paper_with_no_remote_identifier(
    session, cfg: CarrelYAML, tmp_path, monkeypatch
):
    """Papers with only ``pdf_url`` (no DOI/arxiv) can't go through the
    institutional CLI; they should be skipped, not failed."""
    from carrel.sources import remote_downloader as rd_mod

    cfg.storage.root = tmp_path / "data"
    monkeypatch.setattr(rd_mod, "is_configured", lambda: True)
    # A paper with pdf_url only — the SQL filter would already exclude this, but
    # we patch the filter to include it, to exercise the defensive skip.
    p = _seed_remote_candidate(
        session, id="W-only-url",
        doi=None, arxiv_id=None, journal_doi=None, pdf_url="https://example.com/x",
    )

    called = {"n": 0}

    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        called["n"] += 1
        raise AssertionError("process_paper must not be called for no-id paper")

    monkeypatch.setattr(pc, "process_paper", _boom)

    out = pc.fill_closed_papers(
        session, cfg, limit=10, retry_backoff_seconds=0,
    )
    assert called["n"] == 0
    assert out["candidates"] == 1
    assert out["skipped"] == 1
    assert out["parsed"] == 0
    assert out["failed"] == 0
    assert out["retried"] == 0


def test_fill_closed_papers_retries_transient_failures_at_end_of_batch(
    session, cfg: CarrelYAML, tmp_path, monkeypatch
):
    """Transient SSH errors should be retried at the end of the batch and
    increment ``retried`` / ``retried_ok`` accordingly."""
    from carrel.sources import remote_downloader as rd_mod

    cfg.storage.root = tmp_path / "data"
    monkeypatch.setattr(rd_mod, "is_configured", lambda: True)
    # Skip the actual backoff so the test is fast.
    monkeypatch.setattr(pc.time, "sleep", lambda *_a, **_k: None)

    # Three candidates: one already has a PDF on disk (no-op success), one
    # fails transiently *then* succeeds on retry, one fails permanently. After
    # the retry pass, the transient one should be retried and the permanent
    # one should stay failed.
    a = _seed_remote_candidate(session, id="W-ok")
    b = _seed_remote_candidate(session, id="W-transient")
    c = _seed_remote_candidate(session, id="W-perm")

    # Track call order so we can verify the retry happens AFTER the first pass.
    calls: list[str] = []
    pass_counts = {"W-transient": 0, "W-perm": 0, "W-ok": 0}

    def _fake_process(_session, _cfg, paper_id, **_kw):
        calls.append(paper_id)
        if paper_id == "W-ok":
            return None  # success: nothing to update
        if paper_id == "W-transient":
            pass_counts[paper_id] += 1
            if pass_counts[paper_id] == 1:
                # First pass: transient SSH blip.
                raise pc.ProcessError(
                    f"institutional download failed: net blip (attempt 1)"
                ) from rd_mod.RemoteTransientError("net blip")
            # Retry pass: jump host recovered, success.
            return None
        if paper_id == "W-perm":
            pass_counts[paper_id] += 1
            raise pc.ProcessError("institutional download: DOI not found") from (
                rd_mod.RemotePermanentError("DOI not found")
            )
        raise AssertionError(f"unexpected paper_id {paper_id}")

    monkeypatch.setattr(pc, "process_paper", _fake_process)

    out = pc.fill_closed_papers(
        session, cfg, limit=10, retry_backoff_seconds=0,
    )

    # The transient paper was called twice (initial + retry), the permanent
    # one only once.
    assert pass_counts["W-transient"] == 2
    assert pass_counts["W-perm"] == 1
    # Calls order: first pass processes all three, then retry pass processes
    # the transient one.
    assert calls == ["W-ok", "W-transient", "W-perm", "W-transient"]

    # Counters: ok parsed once (not retried), transient parsed on retry, perm
    # failed (not retried).
    assert out["candidates"] == 3
    assert out["parsed"] == 2
    assert out["failed"] == 1
    assert out["skipped"] == 0
    assert out["retried"] == 1
    assert out["retried_ok"] == 1


def test_fill_closed_papers_permanent_failure_not_retried(
    session, cfg: CarrelYAML, tmp_path, monkeypatch
):
    """Permanent errors (rejected identifier) should NOT be retried — they
    would just fail the same way and waste a connection."""
    from carrel.sources import remote_downloader as rd_mod

    cfg.storage.root = tmp_path / "data"
    monkeypatch.setattr(rd_mod, "is_configured", lambda: True)
    monkeypatch.setattr(pc.time, "sleep", lambda *_a, **_k: None)

    p = _seed_remote_candidate(session, id="W-bad")
    call_count = {"n": 0}

    def _fake_process(*_a, **_k):
        call_count["n"] += 1
        raise pc.ProcessError("institutional download: bad") from (
            rd_mod.RemotePermanentError("bad")
        )

    monkeypatch.setattr(pc, "process_paper", _fake_process)

    out = pc.fill_closed_papers(
        session, cfg, limit=10, retry_backoff_seconds=0,
    )

    assert call_count["n"] == 1  # no retry attempted
    assert out["candidates"] == 1
    assert out["failed"] == 1
    assert out["retried"] == 0
    assert out["retried_ok"] == 0


def test_fill_closed_papers_retry_pass_can_be_disabled(
    session, cfg: CarrelYAML, tmp_path, monkeypatch
):
    """``retry_pass=False`` skips the second sweep — same as the legacy
    behavior, kept for callers that want it (e.g. manual /diagnostic runs)."""
    from carrel.sources import remote_downloader as rd_mod

    cfg.storage.root = tmp_path / "data"
    monkeypatch.setattr(rd_mod, "is_configured", lambda: True)

    p = _seed_remote_candidate(session, id="W-transient")
    call_count = {"n": 0}

    def _fake_process(*_a, **_k):
        call_count["n"] += 1
        raise pc.ProcessError("institutional download failed: x") from (
            rd_mod.RemoteTransientError("x")
        )

    monkeypatch.setattr(pc, "process_paper", _fake_process)

    out = pc.fill_closed_papers(
        session, cfg, limit=10, retry_pass=False, retry_backoff_seconds=0,
    )

    assert call_count["n"] == 1
    assert out["retried"] == 0
    assert out["failed"] == 1


def test_fill_closed_papers_retries_when_underlying_error_is_remote_transient(
    session, cfg: CarrelYAML, tmp_path, monkeypatch
):
    """When ``process_paper`` raises a bare ``RemoteTransientError`` (not the
    wrapped ``ProcessError``), the classifier should still recognize it."""
    from carrel.sources import remote_downloader as rd_mod

    cfg.storage.root = tmp_path / "data"
    monkeypatch.setattr(rd_mod, "is_configured", lambda: True)
    monkeypatch.setattr(pc.time, "sleep", lambda *_a, **_k: None)

    p = _seed_remote_candidate(session, id="W-raw-transient")
    call_count = {"n": 0}

    def _fake_process(*_a, **_k):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise rd_mod.RemoteTransientError("ssh down")
        return None  # success on retry

    monkeypatch.setattr(pc, "process_paper", _fake_process)

    out = pc.fill_closed_papers(
        session, cfg, limit=10, retry_backoff_seconds=0,
    )

    assert call_count["n"] == 2
    assert out["parsed"] == 1
    assert out["retried"] == 1
    assert out["retried_ok"] == 1
    assert out["failed"] == 0


def test_fill_closed_papers_backoff_is_honoured(
    session, cfg: CarrelYAML, tmp_path, monkeypatch
):
    """The end-of-batch retry pass must sleep before re-trying, so the jump
    host has time to recover from a brief outage."""
    from carrel.sources import remote_downloader as rd_mod

    cfg.storage.root = tmp_path / "data"
    monkeypatch.setattr(rd_mod, "is_configured", lambda: True)

    p = _seed_remote_candidate(session, id="W-sleep")
    sleeps: list[float] = []

    def _fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    monkeypatch.setattr(pc.time, "sleep", _fake_sleep)

    def _fake_process(*_a, **_k):
        raise pc.ProcessError("institutional download failed: blip") from (
            rd_mod.RemoteTransientError("blip")
        )

    monkeypatch.setattr(pc, "process_paper", _fake_process)

    pc.fill_closed_papers(
        session, cfg, limit=10, retry_backoff_seconds=2.5,
    )

    # Exactly one sleep, and it was the configured backoff.
    assert sleeps == [2.5]


def test_classify_remote_failure_direct_exceptions():
    """The classifier should report the right bucket for both wrapped
    ``ProcessError`` and bare ``Remote*Error`` raises."""
    from carrel.sources import remote_downloader as rd_mod

    assert pc._classify_remote_failure(rd_mod.RemotePermanentError("x")) == "permanent"
    assert pc._classify_remote_failure(rd_mod.RemoteTransientError("x")) == "transient"

    wrapped = pc.ProcessError("institutional download: x")
    wrapped.__cause__ = rd_mod.RemotePermanentError("x")
    assert pc._classify_remote_failure(wrapped) == "permanent"

    wrapped2 = pc.ProcessError("institutional download failed: x")
    wrapped2.__cause__ = rd_mod.RemoteTransientError("x")
    assert pc._classify_remote_failure(wrapped2) == "transient"

    # Unknown error → default to transient so the paper gets another shot.
    assert pc._classify_remote_failure(ValueError("?")) == "transient"


def test_has_remote_identifier_picks_up_each_known_field(session):
    """Mirror of ``process._remote_identifier``; lock in the precedence."""
    # journal_doi wins even if doi is also set
    p = _make_paper(session, id="W-priority", journal_doi="10.1/j", doi="10.1/o", arxiv_id="1234")
    assert pc._has_remote_identifier(p) is True

    # arxiv_id alone is enough
    p2 = _make_paper(session, id="W-arxiv-only", journal_doi=None, doi=None, arxiv_id="1234")
    assert pc._has_remote_identifier(p2) is True

    # pdf_url alone is NOT a remote identifier
    p3 = _make_paper(session, id="W-url-only",
                     journal_doi=None, doi=None, arxiv_id=None, pdf_url="https://example.com")
    assert pc._has_remote_identifier(p3) is False

    # Nothing → false
    p4 = _make_paper(session, id="W-nothing",
                     journal_doi=None, doi=None, arxiv_id=None, pdf_url=None)
    assert pc._has_remote_identifier(p4) is False

