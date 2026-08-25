"""Unit tests for the MCP tool exposure layer.

Covers the four pure functions in :mod:`carrel.mcp.tools`:
``mcp_tool_to_litellm``, ``parse_litellm_name``, ``collect_tools``, and
``dispatch_tool_call``. No subprocess is spawned; everything is fakes.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from carrel.mcp import (
    BUILTIN_SERVER_NAME,
    MCPUnavailable,
    builtin_dispatch_map,
    collect_builtin_tools,
    collect_tools,
    dispatch_tool_call,
    litellm_arguments,
    mcp_tool_to_litellm,
    parse_litellm_name,
)
from mcp.types import CallToolResult, TextContent, Tool


# ---------------------------------------------------------------------------
# mcp_tool_to_litellm / parse_litellm_name
# ---------------------------------------------------------------------------


def test_mcp_tool_to_litellm_prefixes_server():
    tool = Tool(
        name="brave_web_search",
        description="Web search via Brave",
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    out = mcp_tool_to_litellm("brave_search", tool)
    assert out == {
        "type": "function",
        "function": {
            "name": "brave_search__brave_web_search",
            "description": "Web search via Brave",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }


def test_mcp_tool_to_litellm_handles_missing_description():
    tool = Tool(name="x", inputSchema={"type": "object"})
    out = mcp_tool_to_litellm("srv", tool)
    assert out["function"]["description"] == ""
    assert out["function"]["name"] == "srv__x"


def test_parse_litellm_name_round_trips():
    assert parse_litellm_name("brave_search__brave_web_search") == (
        "brave_search",
        "brave_web_search",
    )
    # Even when the tool name itself contains "__", the partition is
    # on the first one so the round-trip with mcp_tool_to_litellm is
    # exact. The two name segments are kept as-is; no validation that
    # the tool portion is the original name.
    server, tool = parse_litellm_name("server__tool__with__underscores")
    assert server == "server"
    assert tool == "tool__with__underscores"


@pytest.mark.parametrize("bad", ["nodelimiter", "__leading", "trailing__", ""])
def test_parse_litellm_name_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_litellm_name(bad)


# ---------------------------------------------------------------------------
# collect_tools
# ---------------------------------------------------------------------------


def test_collect_tools_returns_empty_when_registry_none():
    assert collect_tools(None) == []


def test_collect_tools_flattens_running_clients():
    tool_a = Tool(name="a", description="A", inputSchema={"type": "object"})
    tool_b = Tool(name="b", description="B", inputSchema={"type": "object"})
    tool_c = Tool(name="c", description="C", inputSchema={"type": "object"})

    client1 = MagicMock()
    client1.name = "first"
    client1.is_running = True
    client1.tools = [tool_a, tool_b]

    client2 = MagicMock()
    client2.name = "second"
    client2.is_running = True
    client2.tools = [tool_c]

    client_down = MagicMock()
    client_down.name = "down"
    client_down.is_running = False
    client_down.tools = [Tool(name="ignored", description="", inputSchema={})]

    registry = MagicMock()
    registry.servers.return_value = [client1, client2, client_down]

    out = collect_tools(registry)
    names = [t["function"]["name"] for t in out]
    assert names == ["first__a", "first__b", "second__c"]


# ---------------------------------------------------------------------------
# dispatch_tool_call
# ---------------------------------------------------------------------------


def _make_registry(server: str, tool_name: str, call_result: CallToolResult):
    client = MagicMock()
    client.name = server
    client.is_running = True
    client.call_tool = AsyncMock(return_value=call_result)
    reg = MagicMock()
    reg.get.return_value = client
    return reg, client


def test_dispatch_tool_call_concatenates_text_content():
    result = CallToolResult(
        content=[
            TextContent(type="text", text='{"web": {"results": [{"title": "t", "url": "u"}]}}'),
        ],
        is_error=False,
    )
    reg, client = _make_registry("brave_search", "brave_web_search", result)
    out = asyncio.run(
        dispatch_tool_call(reg, "brave_search__brave_web_search", {"query": "x"})
    )
    assert "t" in out and "u" in out
    client.call_tool.assert_awaited_once_with("brave_web_search", {"query": "x"})


def test_dispatch_tool_call_joins_multiple_text_blocks():
    result = CallToolResult(
        content=[
            TextContent(type="text", text="alpha"),
            TextContent(type="text", text="beta"),
        ],
        is_error=False,
    )
    reg, _ = _make_registry("srv", "tool", result)
    out = asyncio.run(dispatch_tool_call(reg, "srv__tool", {}))
    assert out == "alpha\nbeta"


def test_dispatch_tool_call_marks_upstream_error():
    """The MCP Python SDK exposes ``isError`` (camelCase) on
    ``CallToolResult`` — not ``is_error``. The dispatcher must
    surface upstream error responses so the LLM can see them as
    such (Brave's ``fetch failed`` is the real-world case)."""
    result = CallToolResult(
        content=[TextContent(type="text", text="bad query")],
        isError=True,
    )
    reg, _ = _make_registry("srv", "tool", result)
    out = asyncio.run(dispatch_tool_call(reg, "srv__tool", {}))
    assert "tool error" in out.lower()
    assert "bad query" in out


def test_dispatch_tool_call_marks_upstream_error_snake_alias():
    """A shim layer that sets the older ``is_error`` attribute
    should still be recognized — both spellings read."""
    fake_result = MagicMock()
    fake_result.isError = None
    fake_result.is_error = True
    fake_result.content = [TextContent(type="text", text="boom")]
    reg = MagicMock()
    client = MagicMock()
    client.name = "srv"
    client.is_running = True
    client.call_tool = AsyncMock(return_value=fake_result)
    reg.get.return_value = client
    out = asyncio.run(dispatch_tool_call(reg, "srv__tool", {}))
    assert "tool error" in out.lower()
    assert "boom" in out


def test_dispatch_tool_call_raises_when_server_missing():
    reg = MagicMock()
    reg.get.return_value = None
    with pytest.raises(MCPUnavailable):
        asyncio.run(dispatch_tool_call(reg, "nope__tool", {}))


def test_dispatch_tool_call_raises_when_server_not_running():
    client = MagicMock()
    client.is_running = False
    reg = MagicMock()
    reg.get.return_value = client
    with pytest.raises(MCPUnavailable):
        asyncio.run(dispatch_tool_call(reg, "nope__tool", {}))


def test_dispatch_tool_call_propagates_malformed_name():
    reg = MagicMock()
    with pytest.raises(ValueError):
        asyncio.run(dispatch_tool_call(reg, "no-delimiter", {}))


# ---------------------------------------------------------------------------
# litellm_arguments helper
# ---------------------------------------------------------------------------


def test_litellm_arguments_parses_json_string():
    call = {"function": {"name": "x", "arguments": json.dumps({"q": "hi"})}}
    assert litellm_arguments(call) == {"q": "hi"}


def test_litellm_arguments_passes_through_dict():
    call = {"function": {"name": "x", "arguments": {"q": "hi"}}}
    assert litellm_arguments(call) == {"q": "hi"}


def test_litellm_arguments_returns_empty_on_garbage():
    call = {"function": {"name": "x", "arguments": "not json"}}
    assert litellm_arguments(call) == {}


def test_litellm_arguments_handles_missing_function():
    assert litellm_arguments({}) == {}
    assert litellm_arguments({"function": {}}) == {}


# ---------------------------------------------------------------------------
# Builtins: collect_tools / dispatch_tool_call extensions (M15)
# ---------------------------------------------------------------------------


def test_collect_tools_merges_builtins_before_mcp():
    """When both builtins and MCP tools are available, the builtin litellm
    defs are listed first so the model sees them at the top of its prompt."""
    tool_a = Tool(name="a", description="A", inputSchema={"type": "object"})

    client = MagicMock()
    client.name = "first"
    client.is_running = True
    client.tools = [tool_a]

    registry = MagicMock()
    registry.servers.return_value = [client]

    builtins = collect_builtin_tools()
    out = collect_tools(registry, builtins=builtins)
    names = [t["function"]["name"] for t in out]
    # Builtin(s) come first, then the MCP tools.
    assert names[0].startswith(f"{BUILTIN_SERVER_NAME}__")
    assert "first__a" in names


def test_collect_tools_returns_only_builtins_when_registry_none():
    """When MCP isn't running, we still expose the in-process tools."""
    builtins = collect_builtin_tools()
    out = collect_tools(None, builtins=builtins)
    assert out == builtins
    assert all(t["function"]["name"].startswith(f"{BUILTIN_SERVER_NAME}__")
               for t in out)


def test_collect_tools_empty_without_builtins_and_registry():
    assert collect_tools(None) == []
    # No builtins, no registry → empty list (not None).
    assert collect_tools(None, builtins=[]) == []


def test_dispatch_tool_call_routes_builtin_first():
    """The in-process handler runs even when an MCP server has a tool with
    the same name. The routing rule (builtins win) is the contract."""
    seen: list[tuple[str, dict]] = []

    def _handler(args):
        seen.append(("builtin", args))
        return "builtin-ok"

    # Stub MCP client that would match the same name. If the dispatcher
    # accidentally fell through, this would record a call.
    fake_client = MagicMock()
    fake_client.name = BUILTIN_SERVER_NAME  # would also be a server name
    fake_client.is_running = True
    fake_client.call_tool = AsyncMock(
        side_effect=AssertionError("MCP must not be called for builtin")
    )
    reg = MagicMock()
    reg.get.return_value = fake_client

    out = asyncio.run(
        dispatch_tool_call(
            reg,
            f"{BUILTIN_SERVER_NAME}__save_scholar_note",
            {"slug": "A1", "section_title": "x", "content": "y"},
            builtins={"save_scholar_note": _handler},
        )
    )
    assert out == "builtin-ok"
    assert seen == [("builtin", {"slug": "A1", "section_title": "x", "content": "y"})]


def test_dispatch_tool_call_falls_through_to_mcp_when_no_builtin():
    """When the builtins dict is empty (or the name doesn't match), the
    existing MCP path runs unchanged."""
    result = CallToolResult(
        content=[TextContent(type="text", text="mcp-ok")],
        isError=False,
    )
    reg, client = _make_registry("brave_search", "brave_web_search", result)
    out = asyncio.run(
        dispatch_tool_call(
            reg, "brave_search__brave_web_search", {"query": "x"},
            builtins={},  # empty map → falls through
        )
    )
    assert out == "mcp-ok"
    client.call_tool.assert_awaited_once()


def test_dispatch_tool_call_wraps_builtin_errors_with_tool_error_prefix():
    """Builtin handler errors are surfaced with ``[tool error]`` so the
    wiki_chat wire-format ``is_error`` check (prefix-driven) still works."""

    def _handler(args):
        raise ValueError("invalid scholar slug 'A1'")

    reg = MagicMock()
    out = asyncio.run(
        dispatch_tool_call(
            reg,
            f"{BUILTIN_SERVER_NAME}__save_scholar_note",
            {"slug": "A1", "section_title": "x", "content": "y"},
            builtins={"save_scholar_note": _handler},
        )
    )
    assert out.startswith("[tool error]")
    assert "invalid scholar slug" in out


def test_dispatch_tool_call_raises_when_builtin_name_unknown():
    """A builtin namespace with no matching handler is a misconfiguration,
    not a fall-through — surface as :class:`MCPUnavailable` so the route
    maps to 503 like any other missing server."""
    reg = MagicMock()
    with pytest.raises(MCPUnavailable):
        asyncio.run(
            dispatch_tool_call(
                reg,
                f"{BUILTIN_SERVER_NAME}__no_such_tool",
                {},
                builtins={"save_scholar_note": lambda a: "x"},
            )
        )


def test_builtin_dispatch_map_is_consistent_with_registry():
    """The handler map mirrors the same names :func:`collect_builtin_tools`
    exposes (sans the ``<server>__`` prefix)."""
    handlers = builtin_dispatch_map()
    tools = collect_builtin_tools()
    bare_names = {t["function"]["name"].rsplit("__", 1)[-1] for t in tools}
    assert set(handlers.keys()) == bare_names
