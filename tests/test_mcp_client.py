"""Unit tests for the MCP client lifecycle.

We never spawn a real subprocess. The stdio transport and ClientSession
are replaced with AsyncMock-based fakes that follow the v1 SDK context-
manager shape (``stdio_client(params)`` returns an async cm yielding
``(read, write)``; ``ClientSession(read, write)`` returns an async cm
yielding a session whose methods are coroutines).
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from carrel.mcp import MCPError, MCPUnavailable
from carrel.mcp.client import MCPClient, MCPClientRegistry, reset_mcp
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from mcp import StdioServerParameters

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_session_mock(
    tools: list[Tool] | None = None,
    call_result: CallToolResult | None = None,
    call_side_effect: Any = None,
):
    """Build an AsyncMock that quacks like a v1 ``ClientSession``.

    All three coroutine methods we exercise are set up so callers can
    ``await`` them and either get a canned return or trigger ``side_effect``.
    """
    session = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()
    session.initialize = __import__(
        "unittest.mock", fromlist=["AsyncMock"]
    ).AsyncMock(return_value=None)
    session.list_tools = __import__(
        "unittest.mock", fromlist=["AsyncMock"]
    ).AsyncMock(
        return_value=ListToolsResult(tools=tools or [], nextCursor=None)
    )
    if call_side_effect is not None:
        session.call_tool = __import__(
            "unittest.mock", fromlist=["AsyncMock"]
        ).AsyncMock(side_effect=call_side_effect)
    else:
        session.call_tool = __import__(
            "unittest.mock", fromlist=["AsyncMock"]
        ).AsyncMock(return_value=call_result or CallToolResult(content=[], is_error=False))
    return session


def _patch_stdio(monkeypatch, fake_session):
    """Replace ``stdio_client`` with a function that yields (None, None).

    The session is then obtained via a fake ``ClientSession(read, write)``
    that returns our ``fake_session`` inside its async context manager.
    Both context managers must play nice with ``AsyncExitStack`` — that's
    the only contract the code under test depends on.
    """
    from unittest.mock import MagicMock

    @asynccontextmanager
    async def fake_stdio(_params):
        yield (MagicMock(name="read"), MagicMock(name="write"))

    @asynccontextmanager
    async def fake_session_cm(_read, _write):
        yield fake_session

    monkeypatch.setattr("carrel.mcp.client.stdio_client", fake_stdio)
    monkeypatch.setattr("carrel.mcp.client.ClientSession", fake_session_cm)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_starts_lists_tools_and_tears_down(monkeypatch):
    fake = _make_session_mock(tools=[
        Tool(name="brave_web_search", description="Web search", inputSchema={"type": "object"}),
        Tool(name="brave_local_search", description="Local", inputSchema={"type": "object"}),
    ])
    _patch_stdio(monkeypatch, fake)

    client = MCPClient("test", StdioServerParameters(command="echo"))
    assert not client.is_running
    asyncio.run(client.start())
    assert client.is_running
    assert [t.name for t in client.tools] == ["brave_web_search", "brave_local_search"]
    assert client.last_error is None
    asyncio.run(client.stop())
    assert not client.is_running
    assert client.tools == []


def test_start_failure_records_last_error(monkeypatch):
    """A crash at start time should leave the client not-running and the
    error stashed for the health endpoint to surface."""
    from unittest.mock import MagicMock

    @asynccontextmanager
    async def broken_stdio(_params):
        raise OSError("npx: command not found")
        yield  # pragma: no cover - unreachable, needed to make this an asyncgen

    monkeypatch.setattr("carrel.mcp.client.stdio_client", broken_stdio)
    monkeypatch.setattr("carrel.mcp.client.ClientSession", MagicMock())

    client = MCPClient("test", StdioServerParameters(command="npx"))
    with pytest.raises(MCPError, match="failed to start"):
        asyncio.run(client.start())
    assert not client.is_running
    assert client.last_error is not None
    assert "npx" in client.last_error


def test_call_tool_unavailable_when_not_running():
    client = MCPClient("x", StdioServerParameters(command="echo"))
    with pytest.raises(MCPUnavailable):
        asyncio.run(client.call_tool("any_tool", {}))


def test_call_tool_returns_result(monkeypatch):
    expected = CallToolResult(
        content=[TextContent(type="text", text=json.dumps({
            "web": {"results": [{"title": "t", "url": "u", "description": "d"}]}
        }))],
        is_error=False,
    )
    fake = _make_session_mock(call_result=expected)
    _patch_stdio(monkeypatch, fake)
    client = MCPClient("x", StdioServerParameters(command="echo"))
    asyncio.run(client.start())
    result = asyncio.run(client.call_tool("brave_web_search", {"query": "x"}))
    assert result is expected
    fake.call_tool.assert_awaited_once_with(
        "brave_web_search", {"query": "x"},
    )


def test_call_tool_timeout_raises_mcp_error(monkeypatch):
    """A hanging tool call must surface as MCPError, not hang the test."""
    from unittest.mock import AsyncMock

    session = _make_session_mock()
    session.call_tool = AsyncMock(side_effect=asyncio.TimeoutError)
    _patch_stdio(monkeypatch, session)
    client = MCPClient("x", StdioServerParameters(command="echo"), tool_call_timeout=0.05)
    asyncio.run(client.start())
    with pytest.raises(MCPError, match="timed out"):
        asyncio.run(client.call_tool("any_tool", {}))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_skips_disabled_servers(monkeypatch):
    fake = _make_session_mock(tools=[
        Tool(name="t", description="d", inputSchema={}),
    ])
    _patch_stdio(monkeypatch, fake)
    from carrel.config import MCPServerConfig

    reg = MCPClientRegistry()
    params = StdioServerParameters(command="echo")
    asyncio.run(reg.start_all([
        ("on", MCPServerConfig(command="echo", enabled=True), params),
        ("off", MCPServerConfig(command="echo", enabled=False), params),
    ]))
    assert reg.get("on") is not None
    assert reg.get("off") is None
    asyncio.run(reg.stop_all())
    assert reg.get("on") is None


def test_registry_keeps_other_servers_on_one_failure(monkeypatch):
    """If one server fails to start, the others must still be tracked."""
    from unittest.mock import MagicMock

    @asynccontextmanager
    async def ok_stdio(_params):
        yield (MagicMock(), MagicMock())

    @asynccontextmanager
    async def ok_session(_r, _w):
        yield _make_session_mock(tools=[Tool(name="t", description="d", inputSchema={})])

    @asynccontextmanager
    async def bad_stdio(_params):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    # Patch the SDK names twice via a small indirection: the patched
    # stdio_client switches behavior based on the command in params.
    def switching_stdio(params):
        if params.command == "good":
            return ok_stdio(params)
        return bad_stdio(params)

    monkeypatch.setattr("carrel.mcp.client.stdio_client", switching_stdio)
    monkeypatch.setattr("carrel.mcp.client.ClientSession", ok_session)

    from carrel.config import MCPServerConfig
    reg = MCPClientRegistry()
    good_params = StdioServerParameters(command="good")
    bad_params = StdioServerParameters(command="bad")
    asyncio.run(reg.start_all([
        ("good", MCPServerConfig(command="good", enabled=True), good_params),
        ("bad", MCPServerConfig(command="bad", enabled=True), bad_params),
    ]))
    assert reg.get("good") is not None
    assert reg.get("bad") is None  # never tracked after a failed start
    asyncio.run(reg.stop_all())


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


def test_singleton_is_idempotent(monkeypatch):
    from carrel.config import CarrelYAML, EnvSettings
    from carrel.mcp import client as client_mod
    from carrel.mcp.client import start_mcp

    fake = _make_session_mock(tools=[Tool(name="t", description="d", inputSchema={})])
    _patch_stdio(monkeypatch, fake)
    reset_mcp()
    cfg = CarrelYAML()
    from carrel.config import MCPServerConfig as _MCPServerConfig
    cfg.mcp.servers["x"] = _MCPServerConfig(command="echo", enabled=True)
    env = EnvSettings()
    env.mcp_enabled = True
    r1 = asyncio.run(start_mcp(cfg, env))
    r2 = asyncio.run(start_mcp(cfg, env))
    assert r1 is r2
    asyncio.run(client_mod.stop_mcp())
    reset_mcp()


def test_singleton_disabled_returns_none():
    from carrel.config import CarrelYAML, EnvSettings
    from carrel.mcp.client import start_mcp

    reset_mcp()
    cfg = CarrelYAML()
    cfg.mcp.enabled = False
    env = EnvSettings()
    env.mcp_enabled = True
    assert asyncio.run(start_mcp(cfg, env)) is None
    reset_mcp()
