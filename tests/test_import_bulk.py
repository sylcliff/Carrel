"""Tests for POST /import/bulk — one-shot upsert of N papers.

Each test stubs ``_resolve_work_for_import`` (or the OpenAlex client layer
under it) so we exercise the bulk worker end-to-end without hitting the
network. ``_import_one_paper`` is left intact so the dedup / heal / insert
logic is actually exercised against the in-memory DB.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlmodel import Session

from carrel.models import Job, JobKind, JobStatus, Paper, PaperStatus, SourceKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _oa_work(
    w_id: str = "W1",
    doi: str | None = "10.1/a",
    arxiv: str | None = None,
    title: str = "Paper A",
    venue: str = "NeurIPS",
):
    return {
        "id": f"https://openalex.org/{w_id}",
        "title": title,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "publication_date": "2024-05-01",
        "publication_year": 2024,
        "cited_by_count": 10,
        "primary_location": {
            "source": {"display_name": venue, "type": "conference"}
        },
        "authorships": [
            {"author": {"display_name": "Alice"}, "institutions": []},
        ],
        "abstract_inverted_index": None,
        "open_access": {"is_oa": False},
        "ids": {"arxiv": arxiv} if arxiv else {},
    }


def _s2_record(s2_id: str = "s2abc", doi=None, arxiv=None, title="S2 paper"):
    """Build the S2-shaped dict that ``_s2_record_to_work`` produces."""
    return {
        "_source": "semantic_scholar",
        "id": f"https://www.semanticscholar.org/paper/{s2_id}",
        "s2_paper_id": s2_id,
        "title": title,
        "doi": doi,
        "arxiv_id": arxiv,
        "venue": "Workshop X",
        "publication_date": "2023-01-15",
        "abstract": None,
        "authors": [{"name": "Bob"}],
        "pdf_url": None,
        "citation_count": 3,
        "reference_count": 0,
        "raw": {},
    }


def _patch_resolver(
    monkeypatch, *, by_oa_id=None, by_doi=None, by_arxiv=None, by_s2=None
):
    """Stub ``_resolve_work_for_import`` so we can control per-item outcomes.

    ``by_oa_id`` / ``by_doi`` / ``by_arxiv`` / ``by_s2`` are dicts mapping
    the identifier string → ``(work_dict, source)`` tuple. A lookup that
    misses all maps returns ``None`` (so the item becomes a per-item error).
    """
    import carrel.api.import_bulk as bulk_mod

    def fake(*, oa_id=None, doi=None, arxiv_id=None, s2_id=None, title=None, session=None):
        if oa_id and by_oa_id is not None and oa_id in by_oa_id:
            return by_oa_id[oa_id]
        if doi and by_doi is not None and doi in by_doi:
            return by_doi[doi]
        if arxiv_id and by_arxiv is not None and arxiv_id in by_arxiv:
            return by_arxiv[arxiv_id]
        if s2_id and by_s2 is not None and s2_id in by_s2:
            return by_s2[s2_id]
        return None

    monkeypatch.setattr(bulk_mod, "_resolve_work_for_import", fake)


def _post_bulk(client: TestClient, items, *, background=False):
    return client.post(
        "/import/bulk",
        json={"items": items, "background": background},
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_bulk_happy_path_3_oa_papers(session: Session, client: TestClient, monkeypatch):
    _patch_resolver(
        monkeypatch,
        by_oa_id={
            "W1": (_oa_work(w_id="W1", title="Alpha"), "openalex"),
            "W2": (_oa_work(w_id="W2", title="Beta", doi="10.2/b"), "openalex"),
            "W3": (_oa_work(w_id="W3", title="Gamma", doi="10.3/c"), "openalex"),
        },
    )
    r = _post_bulk(
        client,
        [{"openalex_id": "W1"}, {"openalex_id": "W2"}, {"openalex_id": "W3"}],
        background=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] is not None
    assert len(body["items"]) == 3
    assert all(it["status"] == "ok" and it["created"] for it in body["items"])
    assert [it["id"] for it in body["items"]] == ["W1", "W2", "W3"]
    assert [it["title"] for it in body["items"]] == ["Alpha", "Beta", "Gamma"]

    # The three Paper rows actually exist in the DB.
    for wid in ("W1", "W2", "W3"):
        p = session.get(Paper, wid)
        assert p is not None
        assert p.in_library is True
        assert p.status == PaperStatus.pending.value


# ---------------------------------------------------------------------------
# Mixed sources + partial failure
# ---------------------------------------------------------------------------


def test_bulk_mixed_sources_and_failures(session, client: TestClient, monkeypatch):
    """2 OA + 1 S2-only + 1 unresolvable → 3 ok + 1 error."""
    _patch_resolver(
        monkeypatch,
        by_oa_id={
            "W1": (_oa_work(w_id="W1", title="OA 1"), "openalex"),
            "W2": (_oa_work(w_id="W2", title="OA 2"), "openalex"),
        },
        by_s2={
            "s2only": (_s2_record(s2_id="s2only", title="S2 fallback"), "semantic_scholar"),
        },
    )
    r = _post_bulk(
        client,
        [
            {"openalex_id": "W1"},
            {"openalex_id": "W2"},
            {"s2": "s2only"},
            {"doi": "10.999/nope"},  # not in by_doi → unresolvable
        ],
        background=False,
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 4
    assert [it["status"] for it in items] == ["ok", "ok", "ok", "error"]
    assert items[3]["id"] is None
    assert "not found" in items[3]["error"]
    # The 3 successes are real DB rows.
    assert session.get(Paper, "W1") is not None
    assert session.get(Paper, "s2:s2only") is not None
    # The 1 failure leaves no Paper row.
    assert session.get(Paper, "10.999/nope") is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_bulk_idempotent_reimport(session, client: TestClient, monkeypatch):
    _patch_resolver(
        monkeypatch,
        by_oa_id={
            "W1": (_oa_work(w_id="W1"), "openalex"),
            "W2": (_oa_work(w_id="W2", doi="10.2/b"), "openalex"),
        },
    )
    items = [{"openalex_id": "W1"}, {"openalex_id": "W2"}]

    r1 = _post_bulk(client, items, background=False)
    assert r1.status_code == 200, r1.text
    assert all(it["created"] for it in r1.json()["items"])

    r2 = _post_bulk(client, items, background=False)
    assert r2.status_code == 200, r2.text
    items2 = r2.json()["items"]
    assert all(not it["created"] for it in items2)
    assert all(it["status"] == "ok" for it in items2)


# ---------------------------------------------------------------------------
# Async (background) path
# ---------------------------------------------------------------------------


def test_bulk_async_returns_job_id_only(client: TestClient, monkeypatch):
    """background=true returns {job_id, items=null}; UI polls /sync/jobs/{id}."""
    _patch_resolver(
        monkeypatch,
        by_oa_id={"W1": (_oa_work(w_id="W1"), "openalex")},
    )
    r = _post_bulk(client, [{"openalex_id": "W1"}], background=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["job_id"], int)
    assert body["items"] is None  # async path doesn't fill results inline


def test_bulk_async_job_progress_in_stats(session, client: TestClient, monkeypatch):
    """background=true creates a Job row with initial stats before returning.

    The actual background worker runs in a different SQLAlchemy engine
    (the lifespan-bound ``app_engine``) than the test's in-memory session
    fixture, so we can only assert on what's synchronously committed:
    the Job row + its initial stats. End-to-end progress is covered by
    the inline-mode tests above.
    """
    _patch_resolver(
        monkeypatch,
        by_oa_id={"W1": (_oa_work(w_id="W1", title="Async Alpha"), "openalex")},
    )
    r = _post_bulk(client, [{"openalex_id": "W1"}], background=True)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    job = session.get(Job, job_id)
    assert job is not None
    assert job.kind == JobKind.import_bulk.value
    # Initial stats (set before the worker runs). The worker will mutate
    # these to succeeded/created once it processes the item.
    assert job.stats["total"] == 1
    assert job.stats["succeeded"] == 0
    assert job.stats["current"] is None
    # Job is still queued; the worker hasn't run yet from this test's POV.
    assert job.status == JobStatus.queued.value


# ---------------------------------------------------------------------------
# Zenodo filter
# ---------------------------------------------------------------------------


def test_bulk_zenodo_blocked_per_item(session, client: TestClient, monkeypatch):
    """A work that resolves to a Zenodo deposit → that item is errored, others ok."""
    zenodo_work = _oa_work(w_id="WZEN", title="Zenodo deposit")
    zenodo_work["doi"] = "https://doi.org/10.5281/zenodo.12345"
    _patch_resolver(
        monkeypatch,
        by_oa_id={
            "WOK": (_oa_work(w_id="WOK", title="Good paper"), "openalex"),
            "WZEN": (zenodo_work, "openalex"),
        },
    )
    r = _post_bulk(
        client,
        [{"openalex_id": "WOK"}, {"openalex_id": "WZEN"}],
        background=False,
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[0]["status"] == "ok"
    assert items[1]["status"] == "error"
    assert "Zenodo" in items[1]["error"]
    # Good paper exists, Zenodo does not.
    assert session.get(Paper, "WOK") is not None
    assert session.get(Paper, "WZEN") is None


# ---------------------------------------------------------------------------
# Inbox → library promotion
# ---------------------------------------------------------------------------


def test_bulk_existing_inbox_paper_promoted_to_library(
    session: Session, client: TestClient, monkeypatch
):
    """Paper exists (sync-discovered) but in_library=False → bulk import flips it on."""
    inbox_paper = Paper(
        id="W1",
        id_kind="openalex",
        title="Already known",
        publication_date=date(2024, 1, 1),
        authors=[{"name": "Alice", "openalex_author_id": None, "affiliation": None}],
        status=PaperStatus.pending.value,
        oa_status="none",
        source=SourceKind.openalex.value,
        in_library=False,
        discarded=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(inbox_paper)
    session.commit()

    _patch_resolver(
        monkeypatch,
        by_oa_id={"W1": (_oa_work(w_id="W1", title="Already known"), "openalex")},
    )
    r = _post_bulk(client, [{"openalex_id": "W1"}], background=False)
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["status"] == "ok"
    assert item["created"] is False  # row already existed

    session.refresh(inbox_paper)
    assert inbox_paper.in_library is True
    assert inbox_paper.discarded is False


# ---------------------------------------------------------------------------
# arxiv PDF healing
# ---------------------------------------------------------------------------


def test_bulk_existing_paper_gets_arxiv_pdf_healed(
    session: Session, client: TestClient, monkeypatch
):
    """A paper with arxiv_id but no pdf_url gets the canonical arXiv PDF on import."""
    paper = Paper(
        id="W1",
        id_kind="openalex",
        title="Heal me",
        publication_date=date(2024, 1, 1),
        authors=[],
        arxiv_id="2401.00001",
        pdf_url=None,
        oa_status="none",
        status=PaperStatus.pending.value,
        source=SourceKind.openalex.value,
        in_library=True,
        discarded=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(paper)
    session.commit()

    _patch_resolver(
        monkeypatch,
        by_oa_id={"W1": (_oa_work(w_id="W1", arxiv="2401.00001"), "openalex")},
    )
    r = _post_bulk(client, [{"openalex_id": "W1"}], background=False)
    assert r.status_code == 200, r.text
    session.refresh(paper)
    assert paper.pdf_url == "https://arxiv.org/pdf/2401.00001.pdf"
    assert paper.oa_status == "oa"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_bulk_empty_items_422(client: TestClient):
    r = client.post("/import/bulk", json={"items": [], "background": False})
    assert r.status_code == 422
    # Pydantic min_length=1 message
    assert "at least 1" in r.text or "min_length" in r.text


def test_bulk_too_many_items_422(client: TestClient):
    items = [{"openalex_id": f"W{i}"} for i in range(1001)]
    r = client.post("/import/bulk", json={"items": items, "background": False})
    assert r.status_code == 422
    assert "1000" in r.text or "max_length" in r.text


# ---------------------------------------------------------------------------
# Regression: single /import still works (we refactored _import_one_paper out)
# ---------------------------------------------------------------------------


def test_single_import_still_works_after_refactor(session, client, monkeypatch):
    """Refactor of import_external_paper → _import_one_paper must not break single-import."""
    from carrel.api import search as search_mod

    monkeypatch.setattr(
        search_mod.oa, "lookup_by_doi", lambda doi: _oa_work(w_id="WREFACTOR", doi=doi)
    )
    r = client.post("/import", json={"doi": "10.1/a"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"id": "WREFACTOR", "created": True}
