"""Tests for /scholars/{key}/works: OpenAlex author-works endpoint + library join."""
from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from carrel.api import scholars as scholars_mod
from carrel.models import Paper


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


def _patch_oa(monkeypatch, *, page1=(), page2=(), next_cursor="NEXT"):
    """Patch fetch_author_works to return two pages of pre-canned work dicts."""
    from carrel.api import scholars as sm

    calls = []

    def fake(author_id, *, cursor=None, limit=25):
        calls.append((author_id, cursor, limit))
        if cursor is None:
            return list(page1), next_cursor
        # Second page only returned when a cursor was supplied.
        return list(page2), None

    monkeypatch.setattr(sm.oa, "fetch_author_works", fake)
    return calls


def test_works_returns_page_with_in_library_match(
    session: Session, client: TestClient, monkeypatch
):
    # A library paper that the OpenAlex page 1 will match by openalex_id.
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    # And another author — same name, but not in the works page.
    _patch_oa(
        monkeypatch,
        page1=[
            _oa_work(w_id="W9", doi="10.1/a", title="Already in library"),
            _oa_work(w_id="W200", doi="10.2/b", title="Not yet imported"),
        ],
        next_cursor=None,
    )
    r = client.get("/scholars/A123/works")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["next_cursor"] is None
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
    _patch_oa(
        monkeypatch,
        page1=[_oa_work(w_id="W7", doi="10.1234/arxiv.2401.00001", arxiv="2401.00001")],
        next_cursor=None,
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
    _patch_oa(
        monkeypatch,
        page1=[_oa_work(w_id="W42", doi="10.5555/case")],
        next_cursor=None,
    )
    r = client.get("/scholars/A123/works")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[0]["in_library"] is True
    assert items[0]["library_id"] == "W42"


def test_works_paginates_via_cursor(
    session: Session, client: TestClient, monkeypatch
):
    _seed_paper(session, pid="W1", id_kind="openalex", doi="10.1/a")
    _patch_oa(
        monkeypatch,
        page1=[_oa_work(w_id="W1", doi="10.1/a"), _oa_work(w_id="W2", doi="10.2/b")],
        page2=[_oa_work(w_id="W3", doi="10.3/c")],
        next_cursor="CURSOR-X",
    )
    r1 = client.get("/scholars/A123/works")
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert [it["openalex_id"] for it in body1["items"]] == ["W1", "W2"]
    assert body1["next_cursor"] == "CURSOR-X"

    r2 = client.get("/scholars/A123/works?cursor=CURSOR-X")
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
    _patch_oa(monkeypatch, page1=[])
    r = client.get("/scholars/A_DOES_NOT_EXIST/works")
    assert r.status_code == 404


def test_works_empty_page_returns_empty_items(
    session: Session, client: TestClient, monkeypatch
):
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    _patch_oa(monkeypatch, page1=[], next_cursor=None)
    r = client.get("/scholars/A123/works")
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "next_cursor": None}


def test_works_limit_clamped_to_50(
    session: Session, client: TestClient, monkeypatch
):
    _seed_paper(session, pid="W9", id_kind="openalex", doi="10.1/a")
    _patch_oa(monkeypatch, page1=[])
    r = client.get("/scholars/A123/works?limit=500")
    assert r.status_code == 422  # FastAPI Query(le=50) rejects out-of-range
    r2 = client.get("/scholars/A123/works?limit=50")
    assert r2.status_code == 200


def test_works_openalex_404_scholar_message(
    session: Session, client: TestClient, monkeypatch
):
    # No papers in the library with this A-ID — the aggregation can't find
    # the scholar, so the detail/404 surfaces before the works endpoint runs.
    r = client.get("/scholars/A_NONEXISTENT/works")
    assert r.status_code == 404
