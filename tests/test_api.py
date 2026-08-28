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


def test_paper_detail_etag_and_304(client, session):
    """Layer 1: GET /papers/{id} returns ETag + Cache-Control; re-request with
    If-None-Match returns 304 Not Modified with empty body."""
    session.add(Paper(
        id="W-Etag-Test", id_kind="openalex",
        title="ETag test", venue=None, status="pending", oa_status="closed",
        source="openalex",
        authors=[{"name": "A", "openalex_author_id": "", "affiliation": None}],
        in_library=True,
        created_at=datetime(2026, 8, 28, tzinfo=UTC),
        updated_at=datetime(2026, 8, 28, 0, 0, 0, tzinfo=UTC),
    ))
    session.commit()

    # First request: 200 with ETag.
    r1 = client.get("/papers/W-Etag-Test")
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag is not None
    assert etag.startswith('W/"')
    assert "private" in r1.headers.get("cache-control", "")

    # Same ETag re-requested: 304 Not Modified, empty body.
    r2 = client.get("/papers/W-Etag-Test", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.headers.get("etag") == etag
    assert r2.content == b""

    # After row is mutated, the ETag must change and a stale request 200s.
    # The test mutates the row directly (bypassing the L2 invalidation
    # hooks), so we reset the L2 cache here. The production flow goes
    # through a write endpoint that calls ``invalidate_paper_mutated``;
    # this reset is the test-only equivalent.
    from carrel.api._app_cache import reset_cache_for_tests

    paper = session.get(Paper, "W-Etag-Test")
    paper.updated_at = datetime(2026, 8, 28, 0, 0, 1, tzinfo=UTC)
    session.add(paper)
    session.commit()
    reset_cache_for_tests()

    r3 = client.get("/papers/W-Etag-Test", headers={"If-None-Match": etag})
    assert r3.status_code == 200
    new_etag = r3.headers.get("etag")
    assert new_etag != etag


def test_health_default_omits_cache_stats(client):
    """The default /health probe must stay cheap — no cache stats."""
    r = client.get("/health")
    assert r.status_code == 200
    assert "cache" not in r.json() or r.json().get("cache") is None


def test_health_debug_returns_cache_stats(client):
    """?debug=1 surfaces AppCache.stats() so operators can inspect L2."""
    from carrel.api._app_cache import get_cache

    # Drive the L2 cache to ensure it has at least one entry.
    client.get("/papers?limit=10&offset=0")
    cache = get_cache()

    r = client.get("/health?debug=1")
    assert r.status_code == 200
    body = r.json()
    assert "cache" in body
    stats = body["cache"]
    # The exact keys come from AppCache.stats(); assert the shape.
    assert set(stats.keys()) == {
        "size",
        "maxsize",
        "tags",
        "hits",
        "misses",
        "invalidations",
        "last_status",
    }
    assert stats["maxsize"] == 512
    assert stats["size"] >= 1
    # The list_papers call above just landed, so the decorator should
    # have flipped last_status to either HIT (re-read) or MISS (first).
    assert stats["last_status"] in {"HIT", "MISS"}


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
    # Citation enrichment hits the live Semantic Scholar API; mock it so this
    # smoke test stays hermetic (S2 rate limits / timeouts would otherwise hang
    # the sync through retry backoff).
    with patch("carrel.pipeline.runner.arxiv_src.fetch_recent", return_value=[entry]), \
         patch("carrel.pipeline.runner.oa.lookup_by_arxiv_id", return_value=None), \
         patch("carrel.pipeline.runner.oa.configure"), \
         patch(
             "carrel.pipeline.citations.enrich_papers",
             return_value={"enriched": 0, "failed": 0, "skipped": 1},
         ):
        r = client.post("/sync", json={"lookback_hours": 24, "background": False})

    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] in ("done", "failed")
    assert job["stats"]["new_discovered"] == 1
    rows = session.exec(select(Paper)).all()
    assert len(rows) == 1
    assert rows[0].title == "API-Synced Paper"
    assert rows[0].in_library is False  # sync discovers into the inbox
