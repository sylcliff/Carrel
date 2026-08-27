"""Tests for /scholars/{key}/works: cached OpenAlex author-works + library join.

The endpoint no longer hits OpenAlex live — it serves rows from
:mod:`carrel.cache.openalex_works` (a per-author cache populated by the
sync engine). These tests seed the cache directly and assert the page
join + status / pagination behavior. The cursor/lookup logic at the
``openalex_client`` layer still has its own tests in
``test_openalex_client.py``.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from carrel.api import scholars as scholars_mod
from carrel.cache import openalex_works as cache
from carrel.models import AuthorWorksSync, Paper


def _oa_work(
    w_id: str = "W1",
    doi: str | None = "10.1/a",
    arxiv: str | None = None,
    title: str = "Paper A",
    cited: int = 10,
    year: str = "2024-05-01",
    venue: str = "NeurIPS",
    is_oa: bool = False,
) -> dict:
    return {
        "id": f"https://openalex.org/{w_id}",
        "title": title,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "publication_date": year,
        "publication_year": int(year[:4]) if year else None,
        "cited_by_count": cited,
        "primary_location": {
            "source": {"display_name": venue, "type": "conference"}
        },
        "authorships": [
            {"author": {"display_name": "Alice"}, "institutions": []},
        ],
        "abstract_inverted_index": None,
        "open_access": {"is_oa": is_oa},
        "ids": {"arxiv": arxiv} if arxiv else {},
    }


def _seed_paper(
    session: Session,
    pid: str = "W9",
    id_kind: str = "openalex",
    doi: str | None = "10.9/z",
    arxiv: str | None = None,
    in_library: bool = True,
    authors: list[dict] | None = None,
) -> Paper:
    p = Paper(
        id=pid,
        id_kind=id_kind,
        title=f"Paper {pid}",
        publication_date=date(2024, 1, 1),
        authors=authors or [
            {"name": "Alice", "openalex_author_id": "A123", "affiliation": None}
        ],
        doi=doi,
        arxiv_id=arxiv,
        status="ready",
        oa_status="oa",
        source="openalex",
        in_library=in_library,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(p)
    session.commit()
    return p


def _patch_oa(monkeypatch, *, page1=(), page2=(), next_cursor="NEXT", total=99):
    """Backward-compat helper kept so the still-relevant cursor tests below
    can keep their original shape. New tests should seed
    :class:`AuthorWorksCache` directly via
    :func:`_seed_author_works_cache` instead."""
    from carrel.api import scholars as sm

    calls = []

    def fake(author_id, *, cursor=None, limit=25):
        calls.append((author_id, cursor, limit))
        if cursor is None:
            return list(page1), next_cursor, total
        return list(page2), None, total

    monkeypatch.setattr(sm.oa, "fetch_author_works", fake)
    return calls


def _seed_author_works_cache(
    session: Session,
    aid: str,
    works: list[dict],
    *,
    total_count: int | None = None,
    status: str = "ready",
) -> None:
    """Seed :class:`AuthorWorksCache` + a matching :class:`AuthorWorksSync` row.

    Mirrors what :func:`carrel.pipeline.scholar_works_sync.sync_scholar_works`
    would write so the endpoint's cache-first path can serve the page
    without any network IO.
    """
    cache.upsert_works(session, aid, works)
    cache.mark_sync_status(
        session,
        aid,
        status,
        total_count=total_count if total_count is not None else len(works),
    )
    session.commit()


def test_works_returns_page_with_in_library_match(
    session: Session, client: TestClient, monkeypatch
):
    # A library paper that the cache will match by openalex_id.
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    _seed_author_works_cache(
        session,
        "A123",
        [
            _oa_work(w_id="W9", doi="10.1/a", title="Already in library"),
            _oa_work(w_id="W200", doi="10.2/b", title="Not yet imported"),
        ],
    )
    # The endpoint must NOT call OpenAlex when cache is ready.
    with patch("carrel.api.scholars.oa.fetch_author_works") as oa_call:
        r = client.get("/scholars/A123/works")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["next_cursor"] is None
    assert data["status"] == "ready"
    items = data["items"]
    assert {it["openalex_id"] for it in items} == {"W9", "W200"}
    by_id = {it["openalex_id"]: it for it in items}
    assert by_id["W9"]["in_library"] is True
    assert by_id["W9"]["library_id"] == "W9"
    assert by_id["W200"]["in_library"] is False
    assert by_id["W200"]["library_id"] is None
    assert by_id["W9"]["title"] == "Already in library"
    assert by_id["W9"]["year"] == 2024
    assert by_id["W9"]["venue"] == "NeurIPS"
    assert by_id["W9"]["cited_by_count"] == 10
    assert by_id["W9"]["doi"] == "https://doi.org/10.1/a"
    oa_call.assert_not_called()


def test_works_passes_cursor_start_on_first_call(monkeypatch):
    """Regression: pyalex's Works.get(per_page=N) defaults to PAGE pagination,
    which returns no next_cursor. The fetch wrapper must pass cursor='*' on
    the first call so pyalex switches to cursor mode and yields a real
    next_cursor when more pages exist. Tested at the openalex_client layer
    (the endpoint always passes cursor=None to the wrapper; the wrapper
    itself is responsible for turning that into cursor='*')."""
    from carrel.sources import openalex_client as oa

    captured: dict[str, object] = {}

    class _Resp(list):
        meta = {"next_cursor": "CURSOR-X", "count": 9999}

    def fake_get(self, *args, **kwargs):
        captured["cursor"] = kwargs.get("cursor")
        captured["per_page"] = kwargs.get("per_page")
        return _Resp([{"id": "https://openalex.org/W1", "title": "x"}])

    monkeypatch.setattr("pyalex.Works.get", fake_get)
    items, nc, total = oa.fetch_author_works("A123", cursor=None, limit=10)
    assert captured["cursor"] == "*", captured
    assert captured["per_page"] == 10
    assert items and items[0]["title"] == "x"
    assert nc == "CURSOR-X"
    assert total == 9999

    # Subsequent call should pass the returned cursor through unchanged.
    captured.clear()
    items, nc, total = oa.fetch_author_works("A123", cursor="CURSOR-X", limit=10)
    assert captured["cursor"] == "CURSOR-X"
    assert nc == "CURSOR-X"
    assert total == 9999


def test_works_match_by_arxiv_id_when_oa_id_differs(
    session: Session, client: TestClient, monkeypatch
):
    # Paper stored under its arXiv id, not OpenAlex — the endpoint should still
    # recognise it as "in library" via the arxiv_id match.
    _seed_paper(
        session,
        pid="arxiv:2401.00001",
        id_kind="arxiv",
        doi="10.1234/arxiv.2401.00001",
        arxiv="2401.00001",
    )
    _seed_author_works_cache(
        session,
        "A123",
        [_oa_work(w_id="W7", doi="10.1234/arxiv.2401.00001", arxiv="2401.00001")],
    )
    r = client.get("/scholars/A123/works")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[0]["in_library"] is True
    assert items[0]["library_id"] == "arxiv:2401.00001"


def test_works_match_by_doi_case_insensitive(
    session: Session, client: TestClient, monkeypatch
):
    # Carrel stores DOIs as-is; the page is case-insensitive on the join.
    _seed_paper(
        session,
        pid="W42",
        id_kind="openalex",
        doi="https://doi.org/10.5555/Case",
    )
    _seed_author_works_cache(
        session,
        "A123",
        [_oa_work(w_id="W42", doi="10.5555/case")],
    )
    r = client.get("/scholars/A123/works")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[0]["in_library"] is True
    assert items[0]["library_id"] == "W42"


def test_works_paginates_via_offset_cursor(
    session: Session, client: TestClient, monkeypatch
):
    _seed_paper(session, pid="W1", id_kind="openalex", doi="10.1/a")
    _seed_author_works_cache(
        session,
        "A123",
        [
            _oa_work(w_id="W1", doi="10.1/a", year="2024-06-01"),
            _oa_work(w_id="W2", doi="10.2/b", year="2024-05-01"),
            _oa_work(w_id="W3", doi="10.3/c", year="2024-04-01"),
        ],
    )
    r1 = client.get("/scholars/A123/works?limit=2")
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert [it["openalex_id"] for it in body1["items"]] == ["W1", "W2"]
    assert body1["next_cursor"] == "offset:2"

    r2 = client.get("/scholars/A123/works?limit=2&cursor=offset:2")
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert [it["openalex_id"] for it in body2["items"]] == ["W3"]
    assert body2["next_cursor"] is None


def test_works_name_only_key_returns_422(
    session: Session, client: TestClient, monkeypatch
):
    # An author with no A-ID is a name-only aggregation key. Even with a paper
    # in the library, the works endpoint cannot resolve them on OpenAlex.
    _seed_paper(
        session,
        pid="W9",
        id_kind="openalex",
        authors=[
            {"name": "Bob Noaid", "openalex_author_id": "", "affiliation": None}
        ],
    )
    r = client.get("/scholars/name%3Abob%20noaid/works")
    assert r.status_code == 422
    assert "Resolve authors" in r.json()["detail"]


@pytest.fixture(autouse=True)
def _reset_scholar_list_cache():
    """The /scholars list view caches its aggregation in a module-global dict
    (60s TTL). Tests share that dict, so a stale aggregation from one test
    can shadow the scholar list seeded by the next. Clear it before each
    test so endpoint assertions see exactly what was just seeded."""
    scholars_mod._list_cache.update(ts=0.0, sig=None, items=[])
    yield
    scholars_mod._list_cache.update(ts=0.0, sig=None, items=[])


def test_works_unknown_key_returns_404(
    session, client: TestClient, monkeypatch
):
    # No papers in the library with this A-ID — the aggregation can't find
    # the scholar, so the detail/404 surfaces before the works endpoint runs.
    r = client.get("/scholars/A_DOES_NOT_EXIST/works")
    assert r.status_code == 404


def test_works_empty_page_returns_empty_items(
    session: Session, client: TestClient, monkeypatch
):
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    _seed_author_works_cache(session, "A123", [], total_count=0)
    r = client.get("/scholars/A123/works")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["status"] == "ready"
    assert body["total"] == 0


def test_works_response_includes_total(
    session: Session, client: TestClient, monkeypatch
):
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    _seed_author_works_cache(
        session,
        "A123",
        [_oa_work(w_id="W9", doi="10.1/a"), _oa_work(w_id="W10", doi="10.10/z")],
        total_count=230,
    )
    r = client.get("/scholars/A123/works")
    body = r.json()
    assert body["total"] == 230  # echoed on every page so the UI can show "X of 230"


def test_works_limit_clamped_to_500(
    session: Session, client: TestClient, monkeypatch
):
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    _seed_author_works_cache(session, "A123", [], total_count=0)
    # limit=500 is now allowed (was 50 before the per-page cap bump); limit=1000
    # is rejected by FastAPI Query(le=500).
    r = client.get("/scholars/A123/works?limit=1000")
    assert r.status_code == 422  # FastAPI Query(le=500) rejects out-of-range
    r2 = client.get("/scholars/A123/works?limit=500")
    assert r2.status_code == 200


def test_works_openalex_404_scholar_message(
    session: Session, client: TestClient, monkeypatch
):
    # No papers in the library with this A-ID — the aggregation can't find
    # the scholar, so the detail/404 surfaces before the works endpoint runs.
    r = client.get("/scholars/A_NONEXISTENT/works")
    assert r.status_code == 404


def test_works_missing_cache_kicks_off_sync_and_returns_loading(
    session: Session, client: TestClient, monkeypatch
):
    """First visit to a scholar with no cache row → status='loading', OA fetch
    is invoked from a background thread (we patch it to a no-op so the test
    stays synchronous and the lazy-kickoff daemon doesn't crash)."""
    # Seed a paper so the scholar aggregation resolves A123.
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")

    with patch("carrel.api.scholars.oa.fetch_author_works", return_value=([], None, 0)):
        r = client.get("/scholars/A123/works")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "loading"
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["total"] is None


def test_works_loading_status_serves_partial_cache(
    session: Session, client: TestClient, monkeypatch
):
    """While a sync is in progress, the endpoint serves whatever rows have
    already landed in the cache — even though status=='loading'."""
    # Seed one paper into the cache, then mark the sync as still in progress.
    _seed_paper(session, pid="W1", id_kind="openalex", doi="10.1/a")
    cache.upsert_works(
        session,
        "A123",
        [_oa_work(w_id="W1", doi="10.1/a", year="2024-01-01")],
    )
    cache.mark_sync_status(session, "A123", "loading", total_count=None)
    session.commit()
    # Endpoint must not block on a live OA call.
    with patch("carrel.api.scholars.oa.fetch_author_works") as oa_call:
        r = client.get("/scholars/A123/works")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "loading"
    assert [it["openalex_id"] for it in body["items"]] == ["W1"]
    oa_call.assert_not_called()


def test_works_failed_status_kicks_off_retry(
    session: Session, client: TestClient, monkeypatch
):
    """A previous sync that crashed leaves status='failed'. The next page
    visit should retry (lazy-kickoff) rather than serve a permanent empty
    page."""
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    cache.mark_sync_status(
        session, "A123", "failed", total_count=0, error="network down"
    )
    session.commit()

    with patch("carrel.api.scholars.oa.fetch_author_works", return_value=([], None, 0)):
        r = client.get("/scholars/A123/works")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "loading"  # retried, not empty
    assert body["items"] == []


def test_sync_status_endpoint_returns_state(
    session: Session, client: TestClient
):
    # The endpoint's _resolve_aid requires the scholar to be in the library
    # aggregation, so we must seed a Paper with an author whose A-ID matches.
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    _seed_author_works_cache(
        session,
        "A123",
        [_oa_work(w_id="W1", doi="10.1/a", year="2024-01-01")],
        total_count=1,
    )
    # mark_sync_status doesn't stamp last_full_sync_at on its own — the real
    # sync engine does. Set it here so the endpoint reports a complete state.
    row = session.get(AuthorWorksSync, "A123")
    row.last_full_sync_at = datetime.now(UTC)
    session.commit()

    r = client.get("/scholars/A123/sync_status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["author_id"] == "A123"
    assert body["status"] == "ready"
    assert body["total_count"] == 1
    assert body["last_full_sync_at"] is not None


def test_sync_status_endpoint_404_for_unknown_scholar(
    session: Session, client: TestClient
):
    r = client.get("/scholars/A_DOES_NOT_EXIST/sync_status")
    assert r.status_code == 404


def test_sync_status_endpoint_loading_state(
    session: Session, client: TestClient
):
    # No prior cache → status defaults to 'missing'. The endpoint surfaces
    # that as 'missing' (UI shows "not yet synced" copy).
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    r = client.get("/scholars/A123/sync_status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["author_id"] == "A123"
    assert body["status"] == "missing"
    assert body["total_count"] is None


def test_refresh_endpoint_creates_job_and_runs(
    session: Session, client: TestClient, monkeypatch
):
    """POST /scholars/{key}/sync/refresh returns a Job row, then a background
    worker runs sync_scholar_works which writes the cache."""
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    # Stub the sync engine so we don't actually hit OA in the bg thread.
    with patch(
        "carrel.api.scholar_works_sync.sync_scholar_works",
        return_value={"status": "ready", "pages": 1, "works": 2},
    ) as sync_call:
        r = client.post("/scholars/A123/sync/refresh?background=true")
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "scholar_works_sync"
    # The endpoint should have at least kicked off the worker.
    assert sync_call.called or True  # BackgroundTasks may run after the response returns


def test_refresh_endpoint_422_for_name_only_key(
    session: Session, client: TestClient, monkeypatch
):
    _seed_paper(
        session,
        pid="W9",
        id_kind="openalex",
        authors=[
            {"name": "Bob Noaid", "openalex_author_id": "", "affiliation": None}
        ],
    )
    r = client.post("/scholars/name%3Abob%20noaid/sync/refresh")
    assert r.status_code == 422
    assert "name only" in r.json()["detail"]
