"""Tests for the scholar "Research & enrich" feature (M14.x).

The enrich endpoint is a thin wrapper around
:func:`carrel.pipeline.wiki.scholar_enrich.enrich_scholar_wiki`: an
LLM agent with a whitelisted tool list (``brave_search__brave_web_search``
and ``builtin__save_scholar_note``) appends a ``## Web research`` block to
the scholar page's preserved user-section. These tests exercise the
endpoint and the agent end-to-end with a stubbed LLM stream and a
stubbed MCP registry, so the whole feature can run in-process without
keys or a network.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from carrel import llm
from carrel.models import WikiKind, WikiPage
from carrel.pipeline.wiki import scholar_enrich as scholar_enrich_mod
from carrel.api import wiki as wiki_api


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_scholar_page(
    session,
    *,
    slug: str = "A1234",
    title: str = "Test Scholar",
) -> WikiPage:
    page = WikiPage(
        kind=WikiKind.scholar.value,
        slug=slug,
        title=title,
        path=f"wiki/scholars/{slug}.md",
        summary="",
        stub=False,
        evidence_count=1,
        confidence=0.5,
        links_out=[],
        scholar_aid=slug if slug.startswith("A") else None,
        compiled_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(page)
    session.commit()
    session.refresh(page)
    return page


def _make_concept_page(
    session,
    *,
    slug: str = "rag",
    title: str = "Retrieval-Augmented Generation",
) -> WikiPage:
    page = WikiPage(
        kind=WikiKind.concept.value,
        slug=slug,
        title=title,
        path=f"wiki/concepts/{slug}.md",
        summary="",
        stub=False,
        evidence_count=1,
        confidence=0.5,
        links_out=[],
        compiled_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(page)
    session.commit()
    session.refresh(page)
    return page


def _write_scholar_file(tmp_path: Path, slug: str, body: str) -> Path:
    """Drop a scholar page on disk with a user-section so the agent
    has somewhere to append the note."""
    rel = f"wiki/scholars/{slug}.md"
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\nkind: scholar\ntitle: Test\nslug: {slug}\n---\n"
        f"# {slug}\n\n{body}\n"
        + '\n<section data-user="true"></section>\n'
    )
    full.write_text(text, encoding="utf-8")
    return full


def _patch_mcp_with_brave(monkeypatch, *, call_result):
    """Make ``carrel.pipeline.wiki.scholar_enrich.get_mcp`` return a fake
    registry with one brave_search server."""
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

    monkeypatch.setattr(scholar_enrich_mod, "get_mcp", lambda: registry)
    return registry, client


def _brave_call_result() -> MagicMock:
    """A fake ``CallToolResult`` containing one web-search hit."""
    from mcp.types import TextContent

    result = MagicMock()
    result.isError = None
    result.is_error = False
    result.content = [TextContent(
        type="text",
        text=json.dumps({"web": {"results": [
            {"title": "Prof X joins Y Lab", "url": "https://example.com/x"}
        ]}}),
    )]
    return result


def _brave_error_result(message: str) -> MagicMock:
    """A fake ``CallToolResult`` marked as upstream error."""
    from mcp.types import TextContent

    result = MagicMock()
    result.isError = True
    result.is_error = True
    result.content = [TextContent(type="text", text=message)]
    return result


def _tool_call_chunks(call_id: str, name: str, args: dict) -> list:
    """Build litellm-shaped chunks that assemble into one tool call."""
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


def _text_chunks(text: str) -> list:
    """Build litellm-shaped chunks whose ``delta.content`` is a single
    space-separated token of ``text``."""
    from types import SimpleNamespace

    parts = text.split(" ")
    chunks = []
    for i, part in enumerate(parts):
        token = part + (" " if i < len(parts) - 1 else "")
        chunks.append(SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=token, tool_calls=None,
        ))]))
    return chunks


def _chat_stream_factory(plan: list[list]):
    """Drop-in for ``llm.chat_stream`` that replays ``plan``.

    Each entry in ``plan`` is one "iteration" (a list of chunks).
    """
    from types import SimpleNamespace

    def _accumulate(chunks: list) -> list[dict]:
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
        decoded.append((tokens, _accumulate(chunks)))

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


def _drain_job(client, job_id: int, *, max_iters: int = 30) -> dict:
    """Poll the jobs endpoint until the job lands in a terminal state.
    Returns the final job dict.

    Kept for future tests that exercise the background-task path; the
    current suite runs each enrich inline (``?background=false``) so
    the response itself carries the terminal ``status``.
    """
    import time

    j: dict = {}
    for _ in range(max_iters):
        j = client.get(f"/jobs/{job_id}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.1)
    return j


# ---------------------------------------------------------------------------
# Endpoint shape
# ---------------------------------------------------------------------------


def test_enrich_unknown_page_returns_404(client, session, tmp_path, monkeypatch):
    """No row with the given id → 404."""
    r = client.post("/wiki/pages/9999/enrich")
    assert r.status_code == 404


def test_enrich_non_scholar_page_returns_422(client, session, tmp_path, monkeypatch):
    """Enrich is scholar-only — a concept page must be rejected up front."""
    from carrel.main import app_config

    app_config.storage.root = tmp_path
    _make_concept_page(session)

    r = client.post("/wiki/pages/1/enrich")
    assert r.status_code == 422
    assert "scholar" in r.json()["detail"]


def test_enrich_missing_file_returns_404(client, session, tmp_path, monkeypatch):
    """A scholar row without a file on disk must 404 so the job doesn't
    hard-fail when the in-process ``save_scholar_note`` runs."""
    from carrel.main import app_config

    app_config.storage.root = tmp_path
    _make_scholar_page(session)
    # Note: NO _write_scholar_file call here.

    r = client.post("/wiki/pages/1/enrich")
    assert r.status_code == 404
    assert "recompile" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Agent behavior (background task → poll /jobs/{id})
# ---------------------------------------------------------------------------


def test_enrich_happy_path_writes_web_research_section(
    client, session, tmp_path, monkeypatch
):
    """End-to-end: brave_search returns hits, the agent calls
    save_scholar_note, and the file on disk has a ``## Web research``
    block inside the preserved user-section. Compiled prose above the
    user-section is byte-identical to before.
    """
    from carrel.main import app_config

    app_config.storage.root = tmp_path
    page = _make_scholar_page(session)
    compiled_body = "## Summary\nOriginal compiled prose.\n"
    full = _write_scholar_file(tmp_path, page.slug, compiled_body)
    before_text = full.read_text(encoding="utf-8")

    _patch_mcp_with_brave(monkeypatch, call_result=_brave_call_result())

    note_body = (
        "### Current role & affiliation\n"
        "- Now at [Y Lab](https://example.com/x)\n"
    )
    plan = [
        _tool_call_chunks(
            call_id="call_search",
            name="brave_search__brave_web_search",
            args={"query": "Test Scholar current affiliation"},
        ),
        _tool_call_chunks(
            call_id="call_save",
            name="builtin__save_scholar_note",
            args={"slug": page.slug, "section_title": "Web research", "content": note_body},
        ),
        _text_chunks("Saved Web research note with 1 bullet."),
    ]
    monkeypatch.setattr(llm, "chat_stream", _chat_stream_factory(plan))

    # Run synchronously so the same in-memory engine handles the request
    # and the agent run — the test client uses an in-memory engine
    # that the background-task path can't see.
    r = client.post(
        f"/wiki/pages/{page.id}/enrich?background=false",
    )
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "wiki_enrich"
    assert job["status"] == "done", job

    j = job

    after_text = full.read_text(encoding="utf-8")
    assert "## Web research" in after_text
    assert note_body.strip() in after_text
    # Compiled prose is preserved byte-for-byte.
    assert "Original compiled prose." in after_text

    # The new note is inside the user section, not before it.
    user_idx = after_text.index('<section data-user="true">')
    note_idx = after_text.index("## Web research")
    assert note_idx > user_idx
    # Frontmatter is unchanged.
    assert before_text.split("---")[1] == after_text.split("---")[1]

    # Job stats reflect what the agent did.
    assert "brave_search__brave_web_search" in j["stats"]["tools_called"]
    assert "builtin__save_scholar_note" in j["stats"]["tools_called"]
    assert j["stats"]["notes_saved"] == ["Web research"]
    assert j["stats"]["iterations"] == 3


def test_enrich_graceful_degradation_when_brave_errors(
    client, session, tmp_path, monkeypatch
):
    """If brave_search returns an upstream error, the agent still calls
    save_scholar_note with a one-line 'Could not fetch web results' body
    so the user always sees the button worked. Job ends ``done``.
    """
    from carrel.main import app_config

    app_config.storage.root = tmp_path
    page = _make_scholar_page(session)
    full = _write_scholar_file(tmp_path, page.slug, "## Summary\nCompiled.\n")

    _patch_mcp_with_brave(
        monkeypatch,
        call_result=_brave_error_result("upstream fetch failed"),
    )

    note_body = (
        "Could not fetch web results — check `BRAVE_API_KEY` and the "
        "brave_search MCP server."
    )
    plan = [
        _tool_call_chunks(
            call_id="call_search",
            name="brave_search__brave_web_search",
            args={"query": "Test Scholar"},
        ),
        _tool_call_chunks(
            call_id="call_save",
            name="builtin__save_scholar_note",
            args={"slug": page.slug, "section_title": "Web research", "content": note_body},
        ),
        _text_chunks("Saved Web research note (degraded)."),
    ]
    monkeypatch.setattr(llm, "chat_stream", _chat_stream_factory(plan))

    r = client.post(
        f"/wiki/pages/{page.id}/enrich?background=false",
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "done", j

    after_text = full.read_text(encoding="utf-8")
    assert "## Web research" in after_text
    assert "Could not fetch web results" in after_text
    # The brave_search call was counted as an error in the stats.
    assert (
        "brave_search__brave_web_search" in j["stats"]["errors"]
    ), f"expected brave_search in errors, got {j['stats'].get('errors')!r}"


def test_enrich_does_not_touch_compiled_sections(
    client, session, tmp_path, monkeypatch
):
    """The endpoint only writes to the user-section. Compiled sections
    (``## Summary`` etc.) above the user-section must remain byte-identical
    after the run — the agent does NOT auto-recompile the page.
    """
    from carrel.main import app_config

    app_config.storage.root = tmp_path
    page = _make_scholar_page(session)
    compiled = "## Summary\nOriginal prose.\n## Research lines\nLine 1.\n"
    full = _write_scholar_file(tmp_path, page.slug, compiled)
    user_open = '<section data-user="true">'
    before = full.read_text(encoding="utf-8")
    compiled_before = before.split(user_open)[0]

    _patch_mcp_with_brave(monkeypatch, call_result=_brave_call_result())

    plan = [
        _tool_call_chunks(
            call_id="call_search",
            name="brave_search__brave_web_search",
            args={"query": "Test"},
        ),
        _tool_call_chunks(
            call_id="call_save",
            name="builtin__save_scholar_note",
            args={"slug": page.slug, "section_title": "Web research", "content": "- A fact.\n"},
        ),
        _text_chunks("Done."),
    ]
    monkeypatch.setattr(llm, "chat_stream", _chat_stream_factory(plan))

    r = client.post(
        f"/wiki/pages/{page.id}/enrich?background=false",
    )
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "done", j

    after = full.read_text(encoding="utf-8")
    compiled_after = after.split(user_open)[0]
    assert compiled_after == compiled_before, (
        "compiled prose must be untouched by the enrich run "
        "(the agent only writes inside the user-section)"
    )
    # And the user-section is still present (not removed/replaced).
    assert user_open in after
    assert "</section>" in after


# ---------------------------------------------------------------------------
# Tool filter sanity
# ---------------------------------------------------------------------------


def test_enrich_filters_tools_to_whitelist(
    client, session, tmp_path, monkeypatch
):
    """If the registry exposes a tool *not* in the whitelist, the
    enrich agent must never see it (the litellm call is given only the
    filtered list, so the model can't reach it).
    """
    from mcp.types import Tool

    from carrel.main import app_config

    app_config.storage.root = tmp_path
    page = _make_scholar_page(session)
    _write_scholar_file(tmp_path, page.slug, "## Summary\nCompiled.\n")

    # Build a registry that ALSO exposes a fictional ``code_exec`` tool
    # the agent must not see.
    allowed_tool = Tool(
        name="brave_web_search",
        description="Web search",
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    forbidden_tool = Tool(
        name="code_exec",
        description="Run arbitrary code",
        inputSchema={"type": "object"},
    )
    brave_client = MagicMock()
    brave_client.name = "brave_search"
    brave_client.is_running = True
    brave_client.tools = [allowed_tool]
    brave_client.call_tool = AsyncMock(return_value=_brave_call_result())
    other_client = MagicMock()
    other_client.name = "jailbreak"
    other_client.is_running = True
    other_client.tools = [forbidden_tool]

    registry = MagicMock()
    registry.servers.return_value = [brave_client, other_client]
    registry.get.return_value = brave_client
    monkeypatch.setattr(scholar_enrich_mod, "get_mcp", lambda: registry)

    seen_tool_names: list[str] = []
    real_factory = _chat_stream_factory([
        _tool_call_chunks("c1", "brave_search__brave_web_search", {"query": "Test"}),
        _tool_call_chunks("c2", "builtin__save_scholar_note", {"slug": page.slug, "section_title": "Web research", "content": "- X.\n"}),
        _text_chunks("Done."),
    ])

    def stream(messages, *, tools=None, **_kwargs):
        if tools:
            seen_tool_names.extend(t["function"]["name"] for t in tools)
        yield from real_factory(messages, tools=tools, **_kwargs)

    monkeypatch.setattr(llm, "chat_stream", stream)

    r = client.post(
        f"/wiki/pages/{page.id}/enrich?background=false",
    )
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "done", j

    # The model only ever saw the whitelisted tools — never code_exec.
    assert seen_tool_names  # the agent ran
    assert "jailbreak__code_exec" not in seen_tool_names
    assert "brave_search__brave_web_search" in seen_tool_names
    assert "builtin__save_scholar_note" in seen_tool_names
