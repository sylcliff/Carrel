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
from unittest.mock import MagicMock

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
# MCP tool loop
# ---------------------------------------------------------------------------


def _fake_chat_stream_factory(plan: list[list]):
    """Return a drop-in for ``llm.chat_stream`` that replays ``plan``.

    Each entry in ``plan`` is one "iteration" — a list of chunks the
    real ``chat_stream`` would see. The fake reproduces the real one's
    text-then-on_tool_calls ordering so the wiki chat route can be
    exercised end-to-end without a live LLM.

    The plan shape mirrors what a real model would do across the
    tool loop: iteration 0 emits tool-call deltas, iteration 1 emits
    text deltas after the tool result is appended to the message list.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    def fn(name=None, arguments=None):
        return SimpleNamespace(name=name, arguments=arguments)

    def tc(*, index, id=None, function=None):
        return SimpleNamespace(index=index, id=id, function=function)

    def _accumulate_tool_calls(chunks: list) -> list[dict]:
        accum: dict[int, dict] = {}
        for chunk in chunks:
            try:
                delta = chunk.choices[0].delta
            except (AttributeError, IndexError, KeyError):
                continue
            tc_deltas = getattr(delta, "tool_calls", None)
            if not tc_deltas:
                continue
            for t in tc_deltas:
                idx = getattr(t, "index", None) or 0
                entry = accum.setdefault(idx, {
                    "id": None,
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if getattr(t, "id", None):
                    entry["id"] = t.id
                tfn = getattr(t, "function", None)
                if tfn is not None:
                    if getattr(tfn, "name", None):
                        entry["function"]["name"] += tfn.name
                    if getattr(tfn, "arguments", None):
                        entry["function"]["arguments"] += tfn.arguments
        return [accum[i] for i in sorted(accum)]

    # Pre-compute per-iteration: text tokens to yield + tool calls to fire.
    decoded: list[tuple[list[str], list[dict]]] = []
    for chunks in plan:
        tokens: list[str] = []
        for chunk in chunks:
            try:
                delta = chunk.choices[0].delta
            except (AttributeError, IndexError, KeyError):
                continue
            text = getattr(delta, "content", None)
            if text:
                tokens.append(text)
        decoded.append((tokens, _accumulate_tool_calls(chunks)))

    def fake_stream(messages, **_kwargs):
        step = fake_stream.step  # type: ignore[attr-defined]
        tokens, calls = decoded[step]
        for tok in tokens:
            yield tok
        if _kwargs.get("on_tool_calls") and calls:
            _kwargs["on_tool_calls"](calls)
        fake_stream.step = step + 1  # type: ignore[attr-defined]

    fake_stream.step = 0  # type: ignore[attr-defined]
    return fake_stream


def _patch_mcp(monkeypatch, *, call_result):
    """Make ``carrel.api.wiki_chat.get_mcp`` return a fake registry
    with a single brave_search tool and a stubbed ``call_tool``."""
    from unittest.mock import AsyncMock, MagicMock

    from carrel.api import wiki_chat as wiki_chat_mod
    from mcp.types import Tool

    tool = Tool(
        name="brave_web_search",
        description="Web search",
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    client = MagicMock()
    client.name = "brave_search"
    client.is_running = True
    client.tools = [tool]
    client.call_tool = AsyncMock(return_value=call_result)

    registry = MagicMock()
    registry.servers.return_value = [client]
    registry.get.return_value = client

    monkeypatch.setattr(wiki_chat_mod, "get_mcp", lambda: registry)
    return registry, client


def _patch_mcp_disabled(monkeypatch):
    """No MCP servers available — ``collect_tools`` should return []."""
    from carrel.api import wiki_chat as wiki_chat_mod
    monkeypatch.setattr(wiki_chat_mod, "get_mcp", lambda: None)


def _read_sse_frames(body: str) -> list[dict | str]:
    """Decode the raw SSE body into a flat list of parsed frames."""
    parsed: list[dict | str] = []
    for chunk in body.split("\n\n"):
        for line in chunk.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                parsed.append("DONE")
                continue
            try:
                parsed.append(json.loads(payload))
            except json.JSONDecodeError:
                parsed.append({"_raw": payload})
    return parsed


def _brave_call_result() -> MagicMock:
    """Build a fake ``CallToolResult`` shaped like a Brave web-search
    response. We use ``MagicMock`` instead of a real ``CallToolResult``
    so the existing ``dispatch_tool_call`` test path is exercised.

    Note: ``isError`` MUST be set to ``None`` explicitly. ``MagicMock``
    auto-creates the attribute as a child ``MagicMock`` (which is
    truthy), and the dispatcher's ``isError``-first read treats that
    as an upstream error.
    """
    from mcp.types import TextContent

    result = MagicMock()
    result.isError = None
    result.is_error = False
    result.content = [TextContent(
        type="text",
        text=json.dumps({"web": {"results": [
            {"title": "Latest RAG paper", "url": "https://example.com/rag"}
        ]}}),
    )]
    return result


def _make_tool_call_chunks(call_id: str, name: str, args: dict) -> list:
    """Build litellm-shaped chunks that assemble into one tool call.

    The chunks are split so the real ``chat_stream`` accumulator in
    ``carrel.llm`` has to concatenate the partial JSON argument
    fragments — same as a real provider would emit.
    """
    from types import SimpleNamespace

    arguments = json.dumps(args)

    def fn(name=None, arguments=None):
        return SimpleNamespace(name=name, arguments=arguments)

    def tc(*, index, id=None, function=None):
        return SimpleNamespace(index=index, id=id, function=function)

    n = len(arguments)
    cut1 = max(1, n // 3)
    cut2 = max(cut1 + 1, 2 * n // 3)
    return [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None,
            tool_calls=[tc(index=0, id=call_id, function=fn(name=name, arguments=arguments[:cut1]))],
        ))]),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None,
            tool_calls=[tc(index=0, function=fn(arguments=arguments[cut1:cut2]))],
        ))]),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None,
            tool_calls=[tc(index=0, function=fn(arguments=arguments[cut2:]))],
        ))]),
    ]


def _make_text_chunks(text: str) -> list:
    """Build litellm-shaped chunks whose ``delta.content`` is a single
    space-separated word of ``text``."""
    from types import SimpleNamespace

    parts = text.split(" ")
    chunks = []
    for i, part in enumerate(parts):
        token = part + (" " if i < len(parts) - 1 else "")
        chunks.append(SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=token, tool_calls=None,
        ))]))
    return chunks


def test_post_chat_with_tool_loop_dispatches_and_streams_answer(
    client, session, tmp_path, monkeypatch
):
    from carrel.main import app_config
    app_config.storage.root = tmp_path

    page = _make_page(
        session,
        kind="concept",
        slug="rag",
        title="RAG",
        body="Retrieval-Augmented Generation",
    )
    _write_page_file(tmp_path, page, page.title)

    registry, client_obj = _patch_mcp(
        monkeypatch, call_result=_brave_call_result()
    )

    # Iteration 0: model emits a tool call to brave_web_search.
    # Iteration 1: model emits the final text answer.
    plan = [
        _make_tool_call_chunks(
            call_id="call_abc",
            name="brave_search__brave_web_search",
            args={"query": "RAG news"},
        ),
        _make_text_chunks("Here is the latest RAG news."),
    ]
    fake = _fake_chat_stream_factory(plan)
    monkeypatch.setattr(llm, "chat_stream", fake)
    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda texts, model, batch_size=8: [[0.0] * 2048],
    )

    with client.stream(
        "POST",
        "/wiki/chat",
        json={"messages": [{"role": "user", "content": "What is the latest on RAG?"}]},
    ) as r:
        assert r.status_code == 200
        body = "".join(chunk for chunk in r.iter_text())

    frames = _read_sse_frames(body)
    assert frames[-1] == "DONE"

    # Sources frame first.
    assert "sources" in frames[0]
    tool_call_frames = [f for f in frames if isinstance(f, dict) and f.get("type") == "tool_call"]
    tool_result_frames = [f for f in frames if isinstance(f, dict) and f.get("type") == "tool_result"]
    text_frames = [f for f in frames if isinstance(f, dict) and "t" in f]

    assert len(tool_call_frames) == 1
    assert tool_call_frames[0]["name"] == "brave_search__brave_web_search"
    assert tool_call_frames[0]["args"] == {"query": "RAG news"}

    assert len(tool_result_frames) == 1
    assert tool_result_frames[0]["name"] == "brave_search__brave_web_search"
    assert tool_result_frames[0]["is_error"] is False
    assert "Latest RAG paper" in tool_result_frames[0]["content"]

    # The final text answer was streamed as a series of deltas.
    final_text = "".join(f["t"] for f in text_frames)
    assert "RAG" in final_text

    # The MCP call_tool was actually invoked with the right args.
    client_obj.call_tool.assert_awaited_once_with(
        "brave_web_search", {"query": "RAG news"}
    )
    # And we exercised both plan iterations.
    assert fake.step == 2


def test_post_chat_tool_error_surfaces_is_error_true(
    client, session, tmp_path, monkeypatch
):
    """When the dispatcher returns an ``[tool error] ...`` string
    (because the upstream ``CallToolResult`` had ``isError: True``),
    the SSE ``tool_result`` event MUST carry ``is_error: true`` so the
    UI renders the bubble as an error. The model still sees the
    message and can degrade gracefully — that's why the dispatcher
    prefixes it instead of raising — but the UI's visual signal
    must agree with the prefix.

    Regression: the wire format was previously hardcoded to
    ``is_error = False`` after a successful ``dispatch_tool_call``
    return, regardless of the dispatcher's content.
    """
    from mcp.types import CallToolResult, TextContent

    from carrel.main import app_config

    app_config.storage.root = tmp_path

    # The dispatcher reads ``isError`` (camelCase, MCP spec). A real
    # ``CallToolResult`` with ``isError=True`` is the right shape to
    # exercise the full code path — no monkey-patching required.
    err_result = CallToolResult(
        content=[TextContent(type="text", text="fetch failed")],
        isError=True,
    )
    _patch_mcp(monkeypatch, call_result=err_result)

    # Wiki page is required for the route not to short-circuit with
    # "wiki is empty" before reaching the tool loop.
    page = _make_page(
        session, kind="concept", slug="err", title="Err", body="error path",
    )
    _write_page_file(tmp_path, page, page.title)

    plan = [
        _make_tool_call_chunks(
            call_id="call_err",
            name="brave_search__brave_web_search",
            args={"query": "anything"},
        ),
        _make_text_chunks("Sorry, I couldn't reach the web."),
    ]
    fake = _fake_chat_stream_factory(plan)
    monkeypatch.setattr(llm, "chat_stream", fake)
    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda texts, model, batch_size=8: [[0.0] * 2048],
    )

    with client.stream(
        "POST",
        "/wiki/chat",
        json={"messages": [{"role": "user", "content": "Find something"}]},
    ) as r:
        assert r.status_code == 200
        body = "".join(chunk for chunk in r.iter_text())

    frames = _read_sse_frames(body)
    tool_result_frames = [
        f for f in frames
        if isinstance(f, dict) and f.get("type") == "tool_result"
    ]
    assert len(tool_result_frames) == 1
    assert tool_result_frames[0]["is_error"] is True
    # The dispatcher prefixes the upstream's error message so the
    # model still sees it as text; the wire format mirrors that.
    assert tool_result_frames[0]["content"] == "[tool error] fetch failed"


def test_post_chat_successful_tool_result_is_error_false(
    client, session, tmp_path, monkeypatch
):
    """A successful tool result (no ``[tool error]`` prefix) must
    report ``is_error: false`` on the wire — the symmetric half of
    the regression test above."""
    from carrel.main import app_config

    app_config.storage.root = tmp_path

    _patch_mcp(monkeypatch, call_result=_brave_call_result())

    # Same as above: a wiki page is required for the route to reach
    # the tool loop at all.
    page = _make_page(
        session, kind="concept", slug="ok", title="Ok", body="ok path",
    )
    _write_page_file(tmp_path, page, page.title)

    plan = [
        _make_tool_call_chunks(
            call_id="call_ok",
            name="brave_search__brave_web_search",
            args={"query": "ok"},
        ),
        _make_text_chunks("Done."),
    ]
    fake = _fake_chat_stream_factory(plan)
    monkeypatch.setattr(llm, "chat_stream", fake)
    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda texts, model, batch_size=8: [[0.0] * 2048],
    )

    with client.stream(
        "POST",
        "/wiki/chat",
        json={"messages": [{"role": "user", "content": "Search ok"}]},
    ) as r:
        body = "".join(chunk for chunk in r.iter_text())

    frames = _read_sse_frames(body)
    tool_result_frames = [
        f for f in frames
        if isinstance(f, dict) and f.get("type") == "tool_result"
    ]
    assert tool_result_frames[0]["is_error"] is False


def test_post_chat_no_mcp_falls_back_to_plain_stream(
    client, session, tmp_path, monkeypatch
):
    """When MCP is disabled (registry is None) the route must NOT emit
    any tool events, even if the model would have called a tool. This
    preserves today's behavior for installs without MCP.

    Note: as of M15, the route also exposes in-process builtin tools
    (e.g. ``builtin__save_scholar_note``) which are NOT subprocess-backed
    and run regardless of MCP. The ``tools=`` kwarg is therefore passed
    to ``chat_stream`` here too — what matters for this test is that
    the model never chose to call any tool, so the SSE stream has no
    ``tool_call`` / ``tool_result`` frames.
    """
    from carrel.main import app_config
    app_config.storage.root = tmp_path

    page = _make_page(
        session,
        kind="concept",
        slug="rag",
        title="RAG",
        body="RAG means Retrieval-Augmented Generation",
    )
    _write_page_file(tmp_path, page, page.title)

    _patch_mcp_disabled(monkeypatch)

    def fake_stream(messages, **_kwargs):
        yield "Plain "
        yield "answer"

    monkeypatch.setattr(llm, "chat_stream", fake_stream)
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

    frames = _read_sse_frames(body)
    assert frames[-1] == "DONE"
    tool_frames = [f for f in frames if isinstance(f, dict) and isinstance(f.get("type"), str) and f["type"].startswith("tool_")]
    assert tool_frames == [], f"expected no tool events, got {tool_frames}"
    text_frames = [f for f in frames if isinstance(f, dict) and "t" in f]
    assert "".join(f["t"] for f in text_frames) == "Plain answer"


# ---------------------------------------------------------------------------
# In-process builtin tool (M15) — save_scholar_note end-to-end
# ---------------------------------------------------------------------------


def _write_user_section_page(tmp_path, slug: str, body_before_user: str) -> Path:
    """Write a scholar page with a user section block. ``body_before_user``
    is the compiled prose; the user section sits after it. The user section
    is empty so we can verify the model appends to it."""
    rel = f"wiki/scholars/{slug}.md"
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\nkind: scholar\ntitle: Test\nslug: {slug}\n---\n"
        + body_before_user
        + '\n<section data-user="true"></section>\n'
    )
    full.write_text(text, encoding="utf-8")
    return full


def test_post_chat_save_scholar_note_writes_to_disk(
    client, session, tmp_path, monkeypatch
):
    """End-to-end: the model emits a ``builtin__save_scholar_note`` tool
    call, the route dispatches the in-process handler, and the page on
    disk is mutated to include the new note. Frontmatter and compiled
    prose must be preserved byte-for-byte."""
    from carrel.main import app_config
    from carrel.mcp import BUILTIN_SERVER_NAME

    app_config.storage.root = tmp_path

    page = _make_page(
        session,
        kind="scholar",
        slug="A1234",
        title="Test Scholar",
        body="## Summary\nA noted researcher.\n",
    )
    full = _write_user_section_page(
        tmp_path, "A1234", "## Summary\nA noted researcher.\n"
    )
    before = full.read_text(encoding="utf-8")

    # No MCP servers — we only want the builtin to fire.
    _patch_mcp_disabled(monkeypatch)

    plan = [
        # Iteration 0: the model decides to save a note.
        _make_tool_call_chunks(
            call_id="call_save",
            name=f"{BUILTIN_SERVER_NAME}__save_scholar_note",
            args={
                "slug": "A1234",
                "section_title": "Biographical notes",
                "content": "- Born 1945\n- ETH professor",
            },
        ),
        # Iteration 1: a final text answer.
        _make_text_chunks("Saved."),
    ]
    monkeypatch.setattr(llm, "chat_stream", _fake_chat_stream_factory(plan))
    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda texts, model, batch_size=8: [[0.0] * 2048],
    )

    with client.stream(
        "POST",
        "/wiki/chat",
        json={"messages": [{"role": "user", "content": "Save MP's bio to his page"}]},
    ) as r:
        assert r.status_code == 200
        body = "".join(chunk for chunk in r.iter_text())

    frames = _read_sse_frames(body)
    assert frames[-1] == "DONE"

    # Sources, then a tool_call, then a tool_result, then a token stream.
    assert "sources" in frames[0]
    tool_call_frames = [f for f in frames if isinstance(f, dict) and f.get("type") == "tool_call"]
    tool_result_frames = [f for f in frames if isinstance(f, dict) and f.get("type") == "tool_result"]
    assert len(tool_call_frames) == 1
    assert tool_call_frames[0]["name"] == f"{BUILTIN_SERVER_NAME}__save_scholar_note"
    assert tool_call_frames[0]["args"]["slug"] == "A1234"
    assert tool_call_frames[0]["args"]["section_title"] == "Biographical notes"

    assert len(tool_result_frames) == 1
    assert tool_result_frames[0]["is_error"] is False
    assert "Saved note" in tool_result_frames[0]["content"]
    assert "wiki/scholars/A1234.md" in tool_result_frames[0]["content"]

    # The on-disk file must have the new block AND still have the prior
    # compiled prose + frontmatter.
    after = full.read_text(encoding="utf-8")
    assert "## Biographical notes" in after
    assert "- Born 1945" in after
    assert "- ETH professor" in after
    # Frontmatter preserved.
    assert "kind: scholar" in after
    assert "slug: A1234" in after
    # Compiled prose preserved.
    assert "## Summary" in after
    assert "A noted researcher." in after
    # The new block is inside the user section, not appended to the file
    # body or the frontmatter.
    section_start = after.find('<section data-user="true">')
    section_end = after.find("</section>")
    assert section_start != -1 and section_end != -1
    inside = after[section_start:section_end]
    assert "## Biographical notes" in inside
    # The empty placeholder is gone (replaced by the appended block).
    assert "before" not in before or before != after


def test_post_chat_save_scholar_note_error_surfaces_is_error_true(
    client, session, tmp_path, monkeypatch
):
    """When the in-process handler raises (e.g. invalid slug), the
    dispatcher wraps it with the ``[tool error]`` prefix and the wire
    format MUST flip ``is_error`` to true so the UI renders the bubble
    as an error — the same contract used for upstream MCP failures."""
    from carrel.main import app_config
    from carrel.mcp import BUILTIN_SERVER_NAME

    app_config.storage.root = tmp_path

    page = _make_page(
        session, kind="scholar", slug="A1", title="T", body="b",
    )
    _write_user_section_page(tmp_path, "A1", "## Summary\nb\n")
    _patch_mcp_disabled(monkeypatch)

    plan = [
        _make_tool_call_chunks(
            call_id="call_bad",
            name=f"{BUILTIN_SERVER_NAME}__save_scholar_note",
            # A bad slug — handler raises ValueError.
            args={"slug": "../etc/passwd", "section_title": "x", "content": "y"},
        ),
        _make_text_chunks("I couldn't save that."),
    ]
    monkeypatch.setattr(llm, "chat_stream", _fake_chat_stream_factory(plan))
    monkeypatch.setattr(
        emb_module, "embed_texts",
        lambda texts, model, batch_size=8: [[0.0] * 2048],
    )

    with client.stream(
        "POST",
        "/wiki/chat",
        json={"messages": [{"role": "user", "content": "Save"}]},
    ) as r:
        body = "".join(chunk for chunk in r.iter_text())

    frames = _read_sse_frames(body)
    tool_result_frames = [
        f for f in frames
        if isinstance(f, dict) and f.get("type") == "tool_result"
    ]
    assert len(tool_result_frames) == 1
    assert tool_result_frames[0]["is_error"] is True
    assert tool_result_frames[0]["content"].startswith("[tool error]")
    assert "invalid scholar slug" in tool_result_frames[0]["content"]


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
