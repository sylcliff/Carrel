"""Tests for the global wiki chat backend (M12).

Covers:
- Persistence round-trip (GET / PUT / GET).
- PUT is replace-all (whole-document PUT, like per-paper chat).
- PUT drops invalid/blank turns.
- PUT rejects > 500 turns with 413.
- SSE first frame is a JSON ``sources`` object; then a token stream.
- Empty wiki emits ``{"error": "..."}`` + ``[DONE]`` instead of streaming.
- Retrieval excludes redirect shells.
- The progress-mirror in :mod:`carrel.api.wiki` propagates ``current_index`` /
  ``current_total`` into ``stats[stage]``.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from carrel import embeddings as emb_module
from carrel import llm
from carrel.models import WikiChatMessage, WikiPage
from carrel.api import wiki as wiki_api


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_get_empty_transcript(client):
    r = client.get("/wiki/chat/messages")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["messages"] == []
    assert body["updated_at"] is None


def test_put_replaces_and_returns_transcript(client, session):
    payload = {
        "messages": [
            {"role": "user", "content": "What is RAG?"},
            {"role": "assistant", "content": "RAG is $f(x) = x$."},
        ]
    }
    r = client.put("/wiki/chat/messages", json=payload)
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "What is RAG?"
    assert msgs[1]["content"] == "RAG is $f(x) = x$."

    # Persisted and ordered by id.
    session.expire_all()
    rows = (
        session.query(WikiChatMessage).order_by(WikiChatMessage.id).all()
    )
    assert [(row.role, row.content) for row in rows] == [
        ("user", "What is RAG?"),
        ("assistant", "RAG is $f(x) = x$."),
    ]


def test_put_is_replace_not_append(client):
    client.put(
        "/wiki/chat/messages",
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    r = client.put(
        "/wiki/chat/messages",
        json={"messages": [{"role": "assistant", "content": "second"}]},
    )
    assert r.status_code == 200
    contents = [m["content"] for m in r.json()["messages"]]
    assert contents == ["second"]


def test_put_drops_invalid_and_blank_turns(client, session):
    r = client.put(
        "/wiki/chat/messages",
        json={
            "messages": [
                {"role": "system", "content": "ignored"},
                {"role": "user", "content": "   "},
                {"role": "user", "content": "keep me"},
                {"role": "assistant", "content": "  answer  "},
            ]
        },
    )
    assert r.status_code == 200
    kept = [(m["role"], m["content"]) for m in r.json()["messages"]]
    assert kept == [("user", "keep me"), ("assistant", "answer")]


def test_clear_with_empty_list(client, session):
    client.put(
        "/wiki/chat/messages",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    r = client.put("/wiki/chat/messages", json={"messages": []})
    assert r.status_code == 200
    assert r.json()["messages"] == []
    session.expire_all()
    assert session.query(WikiChatMessage).count() == 0


def test_put_rejects_too_many_turns(client):
    too_many = [{"role": "user", "content": "x"}] * 501
    r = client.put("/wiki/chat/messages", json={"messages": too_many})
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


def _make_page(
    session,
    *,
    kind: str,
    slug: str,
    title: str,
    body: str,
    redirects_to: str | None = None,
    compiled_at: datetime | None = None,
) -> WikiPage:
    path = f"wiki/{kind}s/{slug}.md"
    page = WikiPage(
        kind=kind,
        slug=slug,
        title=title,
        path=path,
        summary="",
        stub=False,
        evidence_count=1,
        confidence=0.5,
        links_out=[],
        redirects_to=redirects_to,
        compiled_at=compiled_at or datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(page)
    session.commit()
    session.refresh(page)
    return page


def _write_page_file(tmp_path, page: WikiPage, body: str) -> None:
    """Drop the page body on disk so _read_body can find it."""
    full = tmp_path / page.path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        f"---\ntitle: {page.title}\n---\n{body}",
        encoding="utf-8",
    )


def test_post_chat_streams_sources_then_tokens(client, session, tmp_path, monkeypatch):
    from carrel.main import app_config
    app_config.storage.root = tmp_path

    page = _make_page(
        session,
        kind="concept",
        slug="rag",
        title="Retrieval-Augmented Generation",
        body="RAG combines a retriever with a generator.",
    )
    _write_page_file(tmp_path, page, page.title)

    # No real LLM — yield a couple of deltas.
    def fake_stream(*_args, **_kwargs):
        yield "Hello"
        yield " world"

    monkeypatch.setattr(llm, "chat_stream", fake_stream)
    # embed_texts shouldn't be called when query is empty enough to fall
    # through to fallback, but we still need a real value to drive top-k.
    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda texts, model, batch_size=8: [[0.0] * 2048],
    )

    with client.stream(
        "POST",
        "/wiki/chat",
        json={"messages": [{"role": "user", "content": "What is RAG?"}]},
    ) as r:
        assert r.status_code == 200
        body = "".join(chunk for chunk in r.iter_text())

    frames = [
        line.removeprefix("data: ").strip()
        for line in body.split("\n\n")
        if line.startswith("data:")
    ]
    parsed = []
    for f in frames:
        if f == "[DONE]":
            parsed.append("DONE")
            continue
        try:
            parsed.append(json.loads(f))
        except json.JSONDecodeError:
            parsed.append({"_raw": f})

    # First non-DONE frame is sources; tokens follow; ends with DONE.
    assert isinstance(parsed[0], dict) and "sources" in parsed[0]
    sources = parsed[0]["sources"]
    assert sources == [{"kind": "concept", "slug": "rag", "title": "Retrieval-Augmented Generation"}]
    token_frames = [p for p in parsed[1:] if isinstance(p, dict) and "t" in p]
    assert [p["t"] for p in token_frames] == ["Hello", " world"]
    assert parsed[-1] == "DONE"


def test_post_chat_empty_wiki_emits_error_frame(client):
    with client.stream(
        "POST",
        "/wiki/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    ) as r:
        assert r.status_code == 200
        body = "".join(chunk for chunk in r.iter_text())

    frames = [
        line.removeprefix("data: ").strip()
        for line in body.split("\n\n")
        if line.startswith("data:")
    ]
    parsed = []
    for f in frames:
        if f == "[DONE]":
            parsed.append("DONE")
            continue
        parsed.append(json.loads(f))
    assert parsed[-1] == "DONE"
    error_frames = [p for p in parsed if isinstance(p, dict) and "error" in p]
    assert error_frames, parsed
    assert "wiki is empty" in error_frames[0]["error"]


def test_post_chat_rejects_request_with_no_user_message(client):
    r = client.post(
        "/wiki/chat",
        json={"messages": [{"role": "assistant", "content": "hi"}]},
    )
    assert r.status_code == 400


def test_retrieval_excludes_redirect_shells(client, session, tmp_path, monkeypatch):
    from carrel.main import app_config
    app_config.storage.root = tmp_path

    live = _make_page(
        session,
        kind="scholar",
        slug="jane-doe",
        title="Jane Doe",
        body="Jane studies RAG.",
    )
    _write_page_file(tmp_path, live, "Jane studies RAG.")
    # A redirect shell — must be filtered out of retrieval.
    _make_page(
        session,
        kind="scholar",
        slug="jd",
        title="JD",
        body="",
        redirects_to="scholar:jane-doe",
    )

    captured: list[list[WikiPage]] = []

    def fake_stream(messages, **_kwargs):
        # Capture the prompt that the server built; we read the page refs
        # from the wrapped context block rather than the message history.
        ctx = next(
            (m["content"] for m in messages if m["role"] == "user"), ""
        )
        captured.append(ctx)
        yield "ok"

    monkeypatch.setattr(llm, "chat_stream", fake_stream)
    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda texts, model, batch_size=8: [[0.0] * 2048],
    )

    with client.stream(
        "POST",
        "/wiki/chat",
        json={"messages": [{"role": "user", "content": "Who is Jane?"}]},
    ):
        pass

    assert captured, "chat_stream was never called"
    ctx = captured[0]
    assert "jane-doe" in ctx
    assert "Page 1 (scholar:jane-doe)" in ctx
    # The redirect shell's slug should not appear as a page header.
    assert "Page 2" not in ctx


# ---------------------------------------------------------------------------
# Progress mirror (Phase C)
# ---------------------------------------------------------------------------


def test_progress_cb_mirrors_index_and_total(monkeypatch):
    """Feeding the wiki progress callback with index/total should mirror
    them into ``stats[stage]['current_index']`` and ``['current_total']``."""
    from datetime import UTC, datetime
    from sqlmodel import Session
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel, create_engine

    from carrel.models import Job, JobKind, JobStatus

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        job = Job(
            kind=JobKind.wiki_compile,
            status=JobStatus.running,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        s.refresh(job)

        cb = wiki_api._make_progress_cb(s, job.id)
        cb({
            "stage": "scholar_compile",
            "index": 3,
            "total": 10,
            "name": "A5002874269",
            "detail": "compiling",
        })
        s.refresh(job)
        assert job.stats is not None
        sub = job.stats["scholar_compile"]
        assert sub["current_index"] == 3
        assert sub["current_total"] == 10
        assert "compiled" not in sub  # only set by a separate counter event
        assert job.stats["stage"] == "scholar_compile"


def test_progress_cb_preserves_existing_counters():
    """Adding a fresh index/total to a sub-dict that already has counts
    must not wipe the prior counters (counters arrive in a separate event)."""
    from datetime import UTC, datetime
    from sqlmodel import Session
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel, create_engine

    from carrel.models import Job, JobKind, JobStatus

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        job = Job(
            kind=JobKind.wiki_compile,
            status=JobStatus.running,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        s.refresh(job)

        cb = wiki_api._make_progress_cb(s, job.id)
        # First event: a counter.
        cb({"stage": "concept_compile", "compiled": 5})
        # Second event: live index/total.
        cb({"stage": "concept_compile", "index": 5, "total": 10, "name": "x"})
        s.refresh(job)
        sub = job.stats["concept_compile"]
        assert sub["compiled"] == 5
        assert sub["current_index"] == 5
        assert sub["current_total"] == 10


def test_progress_cb_captures_recent_io():
    """The pipeline emits ``io={input, output}`` on the final event of each
    item. The callback should keep the last 3 pairs in
    ``stats[stage]["recent"]``, truncated to ~500 chars."""
    from datetime import UTC, datetime
    from sqlmodel import Session
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel, create_engine

    from carrel.models import Job, JobKind, JobStatus

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        job = Job(
            kind=JobKind.wiki_compile,
            status=JobStatus.running,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        s.refresh(job)

        cb = wiki_api._make_progress_cb(s, job.id)
        # Five items; only the last 3 should be kept.
        for i in range(5):
            cb({
                "stage": "scholar_compile",
                "name": f"Scholar {i}",
                "io": {
                    "input": "x" * 1000,           # gets truncated to 500
                    "output": f"summary {i}",
                },
            })
        s.refresh(job)
        recent = job.stats["scholar_compile"]["recent"]
        assert len(recent) == 3
        assert [r["name"] for r in recent] == ["Scholar 2", "Scholar 3", "Scholar 4"]
        # Truncation: input capped at 500 chars.
        assert len(recent[0]["input"]) == 500
        assert recent[-1]["output"] == "summary 4"


def test_progress_cb_io_uses_fallback_name_keys():
    """When ``name`` is absent the callback should fall back to
    ``paper_title``/``title``/``key``/``term`` so paper_extract, concept, and
    question IO all surface a useful label in the UI."""
    from datetime import UTC, datetime
    from sqlmodel import Session
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel, create_engine

    from carrel.models import Job, JobKind, JobStatus

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        job = Job(
            kind=JobKind.wiki_compile,
            status=JobStatus.running,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        s.refresh(job)

        cb = wiki_api._make_progress_cb(s, job.id)
        cb({"stage": "paper_extract", "paper_title": "A Nice Paper",
            "io": {"input": "body", "output": "concept: RAG"}})
        cb({"stage": "concept_compile", "term": "rag", "title": "RAG",
            "io": {"input": "evidence", "output": "summary: ..."}})
        s.refresh(job)
        assert job.stats["paper_extract"]["recent"][0]["name"] == "A Nice Paper"
        assert job.stats["concept_compile"]["recent"][0]["name"] == "RAG"
