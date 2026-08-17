"""API smoke tests: boot the real app against SQLite, exercise routes.

These verify wiring (routes, deps, schemas, the sync trigger) end-to-end.
Network sources are mocked; no Docker/Postgres required.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from carrel.models import Paper, Subscription
from sqlmodel import select


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "up"


def test_subscription_crud(client, session):
    # empty initially
    assert client.get("/subscriptions").json() == []

    # create
    r = client.post("/subscriptions", json={
        "kind": "arxiv_category", "value": "cs.CL", "label": "NLP",
    })
    assert r.status_code == 200, r.text
    sub = r.json()
    assert sub["id"] > 0
    assert sub["kind"] == "arxiv_category"

    # listed
    listed = client.get("/subscriptions").json()
    assert len(listed) == 1 and listed[0]["value"] == "cs.CL"

    # delete
    assert client.delete(f"/subscriptions/{sub['id']}").json() == {"deleted": True}
    assert client.get("/subscriptions").json() == []


def test_subscription_rejects_bad_kind(client):
    r = client.post("/subscriptions", json={"kind": "bogus", "value": "x"})
    assert r.status_code == 400


def test_paper_list_and_detail(client, session):
    paper = Paper(
        id="W1", id_kind="openalex", title="Listed Paper",
        venue="Nature", status="pending", oa_status="closed",
        source="openalex",
        authors=[{"name": "A", "openalex_author_id": "", "affiliation": None}],
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    session.add(paper)
    session.commit()

    listing = client.get("/papers").json()
    assert len(listing) == 1
    assert listing[0]["title"] == "Listed Paper"
    assert listing[0]["authors"] == ["A"]

    detail = client.get("/papers/W1").json()
    assert detail["id"] == "W1"


def test_paper_detail_404(client):
    assert client.get("/papers/nope").status_code == 404


def test_sync_inline_runs_pipeline(client, session):
    from carrel.sources.arxiv import ArxivEntry

    session.add(Subscription(
        kind="arxiv_category", value="cs.CL", enabled=True,
        created_at=datetime.now(UTC),
    ))
    session.commit()

    entry = ArxivEntry(
        arxiv_id="2401.00099", title="API-Synced Paper", summary="s",
        authors=["Z"], categories=["cs.CL"], updated="2026-08-01T00:00:00Z",
        abs_url="https://arxiv.org/abs/2401.00099",
        pdf_url="https://arxiv.org/pdf/2401.00099",
    )
    with patch("carrel.pipeline.runner.arxiv_src.fetch_recent", return_value=[entry]), \
         patch("carrel.pipeline.runner.oa.lookup_by_arxiv_id", return_value=None), \
         patch("carrel.pipeline.runner.oa.configure"):
        r = client.post("/sync", json={"lookback_hours": 24, "background": False})

    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] in ("done", "failed")
    assert job["stats"]["new"] == 1
    rows = session.exec(select(Paper)).all()
    assert len(rows) == 1
    assert rows[0].title == "API-Synced Paper"
