"""Tests for the persistent OpenAlex work cache (A+B+D).

The cache layer is the part of the OpenAlex waste-reduction PR that
intersects with three call paths (scholar page, sync N+1, import
resolver). These tests cover the read-through helpers in
:mod:`carrel.cache.openalex_works` and the sync engine in
:mod:`carrel.pipeline.scholar_works_sync` without any network IO.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from sqlmodel import Session

from carrel.cache import openalex_works as cache
from carrel.models import AuthorWorksCache, AuthorWorksSync, Paper, WorkByArxivId
from carrel.pipeline import scholar_works_sync as sync_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_work(
    *,
    w_id: str,
    title: str,
    doi: str | None = None,
    arxiv: str | None = None,
    year: int = 2024,
    cited: int = 5,
) -> dict:
    """Build a minimal OpenAlex Work dict shaped like pyalex returns it."""
    return {
        "id": f"https://openalex.org/{w_id}",
        "title": title,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "publication_date": f"{year}-01-01",
        "publication_year": year,
        "cited_by_count": cited,
        "primary_location": {"source": {"display_name": "Venue"}},
        "open_access": {"is_oa": False},
        "best_oa_location": {"pdf_url": None, "landing_page_url": None},
        "locations": [],
        "authorships": [],
        "ids": {"arxiv": arxiv} if arxiv else {},
    }


def _seed_paper(
    session: Session,
    *,
    pid: str,
    arxiv: str | None = None,
    doi: str | None = None,
    in_library: bool = True,
    raw_meta: dict | None = None,
) -> Paper:
    p = Paper(
        id=pid,
        id_kind="openalex" if not arxiv else "arxiv",
        title=f"Paper {pid}",
        publication_date=date(2024, 1, 1),
        authors=[{"name": "Alice", "openalex_author_id": "A1", "affiliation": None}],
        doi=doi,
        arxiv_id=arxiv,
        status="ready",
        oa_status="oa",
        source="openalex",
        in_library=in_library,
        raw_meta=raw_meta,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(p)
    session.commit()
    return p


# ---------------------------------------------------------------------------
# lookup_work_by_arxiv_id: 3-layer read-through
# ---------------------------------------------------------------------------


def test_lookup_work_by_arxiv_id_layer1_in_library_paper(session: Session):
    """Layer 1: in-library paper whose raw_meta carries the Work."""
    cached = _make_work(w_id="W10", title="Cached via raw_meta", arxiv="2401.12345")
    _seed_paper(
        session,
        pid="W10",
        arxiv="2401.12345",
        raw_meta={"openalex": cached},
    )
    with patch("carrel.sources.openalex_client.lookup_by_arxiv_id") as oa_call:
        result = cache.lookup_work_by_arxiv_id(session, "2401.12345")
    assert result == cached
    oa_call.assert_not_called()


def test_lookup_work_by_arxiv_id_layer2_cache_table_hit(session: Session):
    """Layer 2: dedicated cache table hit, no live call."""
    cached = _make_work(w_id="W11", title="Cached via work_by_arxiv_id", arxiv="2401.55555")
    session.add(
        WorkByArxivId(
            arxiv_id="2401.55555",
            openalex_id="W11",
            raw_json=cached,
            schema_version=cache.SCHEMA_VERSION,
            fetched_at=datetime.now(UTC),
        )
    )
    session.commit()
    with patch("carrel.sources.openalex_client.lookup_by_arxiv_id") as oa_call:
        result = cache.lookup_work_by_arxiv_id(session, "2401.55555")
    assert result == cached
    oa_call.assert_not_called()


def test_lookup_work_by_arxiv_id_layer3_miss_writes_back(session: Session):
    """Layer 3: live OpenAlex + write-back to work_by_arxiv_id."""
    live = _make_work(w_id="W12", title="From live", arxiv="2401.77777")
    with patch(
        "carrel.sources.openalex_client.lookup_by_arxiv_id",
        return_value=live,
    ) as oa_call:
        result = cache.lookup_work_by_arxiv_id(session, "2401.77777")
    assert result == live
    oa_call.assert_called_once()
    # Write-back happened.
    row = session.get(WorkByArxivId, "2401.77777")
    assert row is not None
    assert row.openalex_id == "W12"
    assert row.raw_json == live


def test_lookup_work_by_arxiv_id_strips_version_suffix(session: Session):
    """'2401.12345v1' and '2401.12345' resolve to the same cache row."""
    cached = _make_work(w_id="W13", title="Versioned", arxiv="2401.12345")
    session.add(
        WorkByArxivId(
            arxiv_id="2401.12345",
            openalex_id="W13",
            raw_json=cached,
            schema_version=cache.SCHEMA_VERSION,
            fetched_at=datetime.now(UTC),
        )
    )
    session.commit()
    with patch("carrel.sources.openalex_client.lookup_by_arxiv_id") as oa_call:
        result = cache.lookup_work_by_arxiv_id(session, "2401.12345v2")
    assert result == cached
    oa_call.assert_not_called()


def test_lookup_work_by_arxiv_id_no_match_returns_none(session: Session):
    """An arXiv id that OpenAlex doesn't have → None, no cache write."""
    with patch(
        "carrel.sources.openalex_client.lookup_by_arxiv_id",
        return_value=None,
    ) as oa_call:
        result = cache.lookup_work_by_arxiv_id(session, "2401.99999")
    assert result is None
    oa_call.assert_called_once()
    # No negative-cache write — caller re-tries later.
    assert session.get(WorkByArxivId, "2401.99999") is None


def test_lookup_work_by_arxiv_id_schema_mismatch_refetches(session: Session):
    """Schema bump → old row ignored, live call re-fires, row rewritten."""
    stale = _make_work(w_id="W14", title="Stale", arxiv="2401.88888")
    session.add(
        WorkByArxivId(
            arxiv_id="2401.88888",
            openalex_id="W14",
            raw_json=stale,
            schema_version=cache.SCHEMA_VERSION - 1,
            fetched_at=datetime.now(UTC),
        )
    )
    session.commit()
    fresh = _make_work(w_id="W14", title="Fresh", arxiv="2401.88888")
    with patch(
        "carrel.sources.openalex_client.lookup_by_arxiv_id",
        return_value=fresh,
    ) as oa_call:
        result = cache.lookup_work_by_arxiv_id(session, "2401.88888")
    assert result == fresh
    oa_call.assert_called_once()
    row = session.get(WorkByArxivId, "2401.88888")
    assert row is not None
    assert row.schema_version == cache.SCHEMA_VERSION
    assert row.raw_json == fresh


# ---------------------------------------------------------------------------
# get_cached_works + upsert_works
# ---------------------------------------------------------------------------


def test_get_cached_works_sort_and_total(session: Session):
    """Newest first, NULLS LAST, tiebreak by cited_by_count."""
    works = [
        _make_work(w_id="W1", title="A", year=2023, cited=1),
        _make_work(w_id="W2", title="B", year=2024, cited=99),
        _make_work(w_id="W3", title="C", year=2024, cited=10),
        _make_work(w_id="W4", title="D", year=2022, cited=0),
    ]
    cache.upsert_works(session, "A100", works)
    session.commit()

    rows, total = cache.get_cached_works(session, "A100", limit=10, offset=0)
    assert total == 4
    assert [r.openalex_id for r in rows] == ["W2", "W3", "W1", "W4"]


def test_get_cached_works_pagination(session: Session):
    """Offset pagination over a 60-row cache."""
    works = [
        _make_work(w_id=f"W{i:03d}", title=f"P{i}", year=2020 + (i % 5))
        for i in range(60)
    ]
    cache.upsert_works(session, "A200", works)
    session.commit()

    page1, total1 = cache.get_cached_works(session, "A200", limit=25, offset=0)
    page2, total2 = cache.get_cached_works(session, "A200", limit=25, offset=25)
    page3, total3 = cache.get_cached_works(session, "A200", limit=25, offset=50)
    assert total1 == total2 == total3 == 60
    assert len(page1) == 25
    assert len(page2) == 25
    assert len(page3) == 10


def test_upsert_works_idempotent(session: Session):
    """Re-upserting the same page refreshes the row, doesn't duplicate it."""
    works = [_make_work(w_id="WX", title="X", year=2024)]
    cache.upsert_works(session, "A300", works)
    session.commit()
    cache.upsert_works(session, "A300", works)
    session.commit()
    rows, total = cache.get_cached_works(session, "A300", limit=10, offset=0)
    assert total == 1
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# AuthorWorksSync state
# ---------------------------------------------------------------------------


def test_mark_sync_status_creates_then_updates(session: Session):
    """Upsert semantics: first call creates, second call updates."""
    cache.mark_sync_status(session, "A400", "loading")
    session.commit()
    row = session.get(AuthorWorksSync, "A400")
    assert row is not None
    assert row.status == "loading"

    cache.mark_sync_status(
        session, "A400", "ready", total_count=42
    )
    session.commit()
    row = session.get(AuthorWorksSync, "A400")
    assert row.status == "ready"
    assert row.total_count == 42


# ---------------------------------------------------------------------------
# sync_scholar_works: pagination + idempotency
# ---------------------------------------------------------------------------


def test_sync_scholar_works_walks_cursor_and_writes_cache(session: Session):
    """Three pages of 50/50/10 → 110 cache rows, status=ready."""
    pages = [
        [_make_work(w_id=f"W{p}_{i:03d}", title=f"p{p}-{i}") for i in range(50)]
        for p in range(2)
    ]
    pages.append(
        [_make_work(w_id=f"W2_{i:03d}", title=f"p2-{i}") for i in range(10)]
    )
    cursors = ["CUR-1", "CUR-2", None]
    totals = [110, 110, 110]

    def fake_fetch(author_id, *, cursor=None, limit=25):
        if cursor is None or cursor == "*":
            idx = 0
        else:
            idx = cursors.index(cursor) + 1
        return pages[idx], cursors[idx], totals[idx]

    with patch(
        "carrel.pipeline.scholar_works_sync.oa.fetch_author_works",
        side_effect=fake_fetch,
    ) as call:
        result = sync_engine.sync_scholar_works(
            session, "A500", on_progress=None
        )
    assert call.call_count == 3
    assert result["status"] == "ready"
    assert result["pages"] == 3
    assert result["works"] == 110

    rows, total = cache.get_cached_works(session, "A500", limit=200, offset=0)
    assert total == 110
    assert len(rows) == 110
    state = session.get(AuthorWorksSync, "A500")
    assert state is not None
    assert state.status == "ready"
    assert state.total_count == 110
    assert state.last_full_sync_at is not None


def test_sync_scholar_works_idempotent_when_already_loading(session: Session):
    """Second concurrent call sees status=loading and returns without a fetch."""
    cache.mark_sync_status(session, "A600", "loading")
    session.commit()
    with patch(
        "carrel.pipeline.scholar_works_sync.oa.fetch_author_works"
    ) as call:
        result = sync_engine.sync_scholar_works(
            session, "A600", on_progress=None
        )
    call.assert_not_called()
    assert result["status"] == "skipped"
    assert result["reason"] == "already_loading"


def test_sync_scholar_works_force_bypasses_loading_guard(session: Session):
    """The manual refresh path uses force=True to re-run regardless."""
    cache.mark_sync_status(session, "A700", "loading")
    session.commit()
    with patch(
        "carrel.pipeline.scholar_works_sync.oa.fetch_author_works",
        return_value=([], None, 0),
    ) as call:
        result = sync_engine.sync_scholar_works(
            session, "A700", on_progress=None, force=True
        )
    call.assert_called_once()
    assert result["status"] == "ready"


def test_sync_scholar_works_marks_failed_on_exception(session: Session):
    """An OA failure flips the row to 'failed' and re-raises."""
    with patch(
        "carrel.pipeline.scholar_works_sync.oa.fetch_author_works",
        side_effect=RuntimeError("network down"),
    ):
        with pytest.raises(RuntimeError):
            sync_engine.sync_scholar_works(session, "A800", on_progress=None)
    row = session.get(AuthorWorksSync, "A800")
    assert row is not None
    assert row.status == "failed"
    assert "network down" in (row.last_error or "")
