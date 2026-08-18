"""Tests for the /search/semantic endpoint (M5)."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from carrel.models import Chunk, Paper, PaperStatus
from fastapi.testclient import TestClient


def _make_paper(session, *, pid: str, title: str, vec: list[float] | None = None) -> Paper:
    p = Paper(
        id=pid,
        id_kind="openalex",
        title=title,
        status=PaperStatus.ready.value,
        oa_status="oa",
        source="openalex",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(p)
    session.flush()
    if vec is not None:
        session.add(Chunk(
            paper_id=pid,
            chunk_index=0,
            content_md=f"body of {title}",
            embedding=vec,
        ))
    session.commit()
    return p


def test_semantic_search_returns_ranked_results(session, client: TestClient):
    """Two papers, query is closer to paper A. A should rank first."""
    # 2-D vectors so cosine is easy to eyeball
    _make_paper(session, pid="W-A", title="Alpha paper", vec=[1.0, 0.0])
    _make_paper(session, pid="W-B", title="Beta paper", vec=[0.0, 1.0])
    session.commit()

    fake_query_vec = [0.9, 0.1]  # closer to A

    def _fake_embed(texts, **kwargs):
        return [fake_query_vec for _ in texts]

    with patch("carrel.api.search.emb.embed_texts", _fake_embed):
        r = client.get("/search/semantic", params={"q": "anything"})

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["query"] == "anything"
    assert len(data["results"]) == 2
    assert data["results"][0]["id"] == "W-A"
    assert data["results"][0]["best_score"] > data["results"][1]["best_score"]
    # Top-3 chunks per paper
    assert len(data["results"][0]["hits"]) >= 1
    assert data["results"][0]["hits"][0]["paper_id"] == "W-A"


def test_semantic_search_groups_multiple_chunks_per_paper(session, client: TestClient):
    pid = "W-multi"
    _make_paper(session, pid=pid, title="Multi", vec=[1.0, 0.0])
    session.add(
        Chunk(paper_id=pid, chunk_index=1, content_md="second", embedding=[0.95, 0.05])
    )
    session.add(
        Chunk(paper_id=pid, chunk_index=2, content_md="third", embedding=[0.0, 1.0])
    )  # lower
    session.commit()

    def _fake_embed(texts, **kwargs):
        return [[0.9, 0.1] for _ in texts]

    with patch("carrel.api.search.emb.embed_texts", _fake_embed):
        r = client.get("/search/semantic", params={"q": "x"})

    data = r.json()
    assert len(data["results"]) == 1
    hits = data["results"][0]["hits"]
    # Top 3 chunks per paper are returned; best first.
    assert len(hits) == 3
    assert hits[0]["score"] >= hits[1]["score"] >= hits[2]["score"]


def test_semantic_search_empty_query_returns_empty(session, client: TestClient):
    r = client.get("/search/semantic", params={"q": ""})
    assert r.status_code == 200
    assert r.json() == {"query": "", "results": []}


def test_semantic_search_embedding_failure_returns_empty(session, client: TestClient):
    _make_paper(session, pid="W-X", title="X", vec=[0.1, 0.2])

    def _boom(texts, **kwargs):
        raise RuntimeError("API down")

    with patch("carrel.api.search.emb.embed_texts", _boom):
        r = client.get("/search/semantic", params={"q": "anything"})

    assert r.status_code == 200
    assert r.json()["results"] == []


def test_semantic_search_skips_papers_without_chunks(session, client: TestClient):
    """Papers with no embedding rows should not appear."""
    _make_paper(session, pid="W-embed", title="Has chunks", vec=[1.0, 0.0])
    _make_paper(session, pid="W-noembed", title="No chunks")  # no vec

    def _fake_embed(texts, **kwargs):
        return [[0.9, 0.1] for _ in texts]

    with patch("carrel.api.search.emb.embed_texts", _fake_embed):
        r = client.get("/search/semantic", params={"q": "x"})

    ids = {row["id"] for row in r.json()["results"]}
    assert ids == {"W-embed"}


def test_semantic_search_respects_limit(session, client: TestClient):
    for i in range(5):
        _make_paper(session, pid=f"W-{i}", title=f"P{i}", vec=[float(i), 0.0])

    def _fake_embed(texts, **kwargs):
        return [[0.9, 0.1] for _ in texts]

    with patch("carrel.api.search.emb.embed_texts", _fake_embed):
        r = client.get("/search/semantic", params={"q": "x", "limit": 2})

    assert len(r.json()["results"]) == 2
