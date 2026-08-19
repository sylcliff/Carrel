"""Tests for the ad-hoc arXiv search function."""
from __future__ import annotations

import httpx
import pytest

from carrel.sources import arxiv as arxiv_src


ATOM_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <updated>2024-01-15T00:00:00Z</updated>
    <title>Retrieval Augmented Generation for Tests</title>
    <summary>We do RAG in tests.</summary>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <link title="pdf" type="application/pdf" href="http://arxiv.org/pdf/2401.00001"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00002v1</id>
    <updated>2024-01-10T00:00:00Z</updated>
    <title>Another RAG Paper</title>
    <summary>Also RAG.</summary>
    <author><name>Carol</name></author>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <link title="pdf" type="application/pdf" href="http://arxiv.org/pdf/2401.00002"/>
  </entry>
</feed>
"""


def _make_client(atom: str = ATOM_TEMPLATE):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=atom)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_search_returns_entries(monkeypatch):
    client = _make_client()
    # Bypass the function's own client construction to use the mock.
    import carrel.sources.arxiv as arxiv_mod
    monkeypatch.setattr(
        arxiv_mod.httpx, "Client", lambda *a, **k: client
    )
    results = arxiv_src.search("retrieval augmented", limit=10)
    assert len(results) == 2
    assert results[0].arxiv_id == "2401.00001v2"
    assert results[0].title == "Retrieval Augmented Generation for Tests"
    assert results[0].authors == ["Alice", "Bob"]
    assert results[0].pdf_url == "http://arxiv.org/pdf/2401.00001"


def test_search_empty_query_returns_empty():
    assert arxiv_src.search("") == []


def test_search_passes_categories(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.query
        captured["query"] = q.decode() if isinstance(q, bytes) else q
        return httpx.Response(200, content=ATOM_TEMPLATE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    import carrel.sources.arxiv as arxiv_mod
    monkeypatch.setattr(arxiv_mod.httpx, "Client", lambda *a, **k: client)

    arxiv_src.search("rag", categories=["cs.CL", "cs.LG"])
    assert "cs.CL" in captured["query"]
    assert "cs.LG" in captured["query"]
    assert "sortBy=relevance" in captured["query"]


def test_rate_exceeded_body_retries(monkeypatch):
    # First response is 200 with "Rate exceeded." body; second is real feed.
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text="Rate exceeded. Please retry.")
        return httpx.Response(200, content=ATOM_TEMPLATE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    import carrel.sources.arxiv as arxiv_mod
    monkeypatch.setattr(arxiv_mod.httpx, "Client", lambda *a, **k: client)
    monkeypatch.setattr(arxiv_mod.time, "sleep", lambda *_a, **_k: None)

    results = arxiv_src.search("rag", limit=10)
    assert len(results) == 2
    assert calls["n"] == 2
