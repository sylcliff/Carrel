"""Smoke tests for the Brave web search route + MCP debug endpoints.

Strategy: the ``client`` fixture (conftest) boots the real FastAPI app
against an in-memory SQLite. We then *replace* ``get_mcp`` (and a
private accessor in the brave route) with a fake that returns canned
``CallToolResult`` content, so no real subprocess is ever spawned.

These tests also exercise the request/response shape that the frontend
will receive — they're the contract tests for the new endpoints.
"""
from __future__ import annotations

import json

from carrel import mcp as mcp_mod
from carrel.api import search_brave as sb_mod
from carrel.mcp import MCPClientRegistry, MCPError, MCPUnavailable, reset_mcp
from mcp.types import CallToolResult, TextContent, Tool

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMCPClient:
    """A stand-in for :class:`carrel.mcp.client.MCPClient`.

    Only implements the bits the route cares about: ``is_running`` and
    ``call_tool``. The ``tools`` attribute is set by the health-endpoint
    tests.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        call_result: CallToolResult | None = None,
        call_side_effect: Exception | None = None,
        tools: list[Tool] | None = None,
        is_running: bool = True,
    ):
        self.name = name
        self._call_result = call_result
        self._call_side_effect = call_side_effect
        self.tools = tools or []
        self.is_running = is_running
        self.last_error: str | None = None

    async def call_tool(self, name: str, arguments: dict):
        if self._call_side_effect is not None:
            raise self._call_side_effect
        return self._call_result


def _brave_web_payload(results: list[dict]) -> CallToolResult:
    """Build a CallToolResult that looks like @brave/brave-search-mcp-server output."""
    return CallToolResult(
        content=[TextContent(
            type="text",
            text=json.dumps({"web": {"results": results}}),
        )],
        is_error=False,
    )


# ---------------------------------------------------------------------------
# /mcp/health
# ---------------------------------------------------------------------------


def test_mcp_health_when_disabled(client, monkeypatch):
    """MCP off → empty server list, enabled=False, no error."""
    from carrel.main import app_env  # noqa: PLC0415

    monkeypatch.setattr(app_env, "mcp_enabled", False)
    reset_mcp()
    r = client.get("/mcp/health")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["servers"] == []
    assert body["error"] is None


def test_mcp_health_when_running(client, monkeypatch):
    """One server up with tools → reported as running with the right count."""
    from carrel.main import app_env  # noqa: PLC0415

    monkeypatch.setattr(app_env, "mcp_enabled", True)
    reset_mcp()
    fake_reg = MCPClientRegistry()
    fake_reg._clients["brave_search"] = _FakeMCPClient(tools=[
        Tool(name="brave_web_search", description="d", inputSchema={"type": "object"}),
        Tool(name="brave_local_search", description="d", inputSchema={"type": "object"}),
    ])
    monkeypatch.setattr(mcp_mod, "get_mcp", lambda: fake_reg)
    # Make the late-import path in api/mcp.py also return the same fake.
    monkeypatch.setattr("carrel.api.mcp.get_mcp", lambda: fake_reg)

    r = client.get("/mcp/health")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    servers = {s["name"]: s for s in body["servers"]}
    assert servers["brave_search"]["running"] is True
    assert servers["brave_search"]["tool_count"] == 2


# ---------------------------------------------------------------------------
# /mcp/tools
# ---------------------------------------------------------------------------


def test_mcp_tools_lists_all_servers(client, monkeypatch):
    reset_mcp()
    fake_reg = MCPClientRegistry()
    fake_reg._clients["brave_search"] = _FakeMCPClient(
        name="brave_search",
        tools=[Tool(
            name="brave_web_search",
            description="Search the web",
            inputSchema={"type": "object"},
        )],
    )
    monkeypatch.setattr(mcp_mod, "get_mcp", lambda: fake_reg)
    monkeypatch.setattr("carrel.api.mcp.get_mcp", lambda: fake_reg)

    r = client.get("/mcp/tools")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "brave_web_search"
    assert body[0]["server"] == "brave_search"
    assert body[0]["description"] == "Search the web"


def test_mcp_tools_empty_when_disabled(client, monkeypatch):
    reset_mcp()
    monkeypatch.setattr(mcp_mod, "get_mcp", lambda: None)
    monkeypatch.setattr("carrel.api.mcp.get_mcp", lambda: None)
    r = client.get("/mcp/tools")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# /search/brave
# ---------------------------------------------------------------------------


def test_brave_search_success(client, monkeypatch):
    reset_mcp()
    fake_reg = MCPClientRegistry()
    fake_reg._clients["brave_search"] = _FakeMCPClient(call_result=_brave_web_payload([
        {"title": "OpenWiki", "url": "https://example.com/openwiki",
         "description": "auto-generated code wiki", "age": "2 days ago",
         "language": "en", "family_friendly": True,
         "extra_snippets": ["another snippet"]},
        {"title": "NoDesc", "url": "https://example.com/nodesc"},
    ]))
    monkeypatch.setattr(mcp_mod, "get_mcp", lambda: fake_reg)
    monkeypatch.setattr(sb_mod, "get_mcp", lambda: fake_reg)

    r = client.post("/search/brave", json={"query": "OpenWiki", "count": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "OpenWiki"
    assert body["total"] == 2
    assert body["results"][0]["title"] == "OpenWiki"
    assert body["results"][0]["extra_snippets"] == ["another snippet"]
    assert body["results"][1]["description"] is None
    assert body["took_ms"] >= 0


def test_brave_search_unavailable_returns_503(client, monkeypatch):
    """Registry up but the brave_search server isn't there → 503, not 500."""
    reset_mcp()
    fake_reg = MCPClientRegistry()  # empty
    monkeypatch.setattr(mcp_mod, "get_mcp", lambda: fake_reg)
    monkeypatch.setattr(sb_mod, "get_mcp", lambda: fake_reg)

    r = client.post("/search/brave", json={"query": "x"})
    assert r.status_code == 503
    assert "brave_search" in r.json()["detail"]


def test_brave_search_mcp_disabled_returns_503(client, monkeypatch):
    reset_mcp()
    monkeypatch.setattr(mcp_mod, "get_mcp", lambda: None)
    monkeypatch.setattr(sb_mod, "get_mcp", lambda: None)
    r = client.post("/search/brave", json={"query": "x"})
    assert r.status_code == 503


def test_brave_search_timeout_returns_502(client, monkeypatch):
    """MCPError (e.g. timeout) maps to 502 — upstream is broken, retrying
    after a restart won't help."""
    reset_mcp()
    fake_reg = MCPClientRegistry()
    fake_reg._clients["brave_search"] = _FakeMCPClient(
        call_side_effect=MCPError("tool call timed out after 30s"),
    )
    monkeypatch.setattr(mcp_mod, "get_mcp", lambda: fake_reg)
    monkeypatch.setattr(sb_mod, "get_mcp", lambda: fake_reg)

    r = client.post("/search/brave", json={"query": "x"})
    assert r.status_code == 502
    assert "timed out" in r.json()["detail"]


def test_brave_search_subprocess_died_between_check_and_call(client, monkeypatch):
    """If the subprocess dies mid-call, we still surface 503 (not 500)."""
    reset_mcp()
    fake_reg = MCPClientRegistry()
    fake_reg._clients["brave_search"] = _FakeMCPClient(
        call_side_effect=MCPUnavailable("subprocess died"),
    )
    monkeypatch.setattr(mcp_mod, "get_mcp", lambda: fake_reg)
    monkeypatch.setattr(sb_mod, "get_mcp", lambda: fake_reg)

    r = client.post("/search/brave", json={"query": "x"})
    assert r.status_code == 503


def test_brave_search_request_validation(client, monkeypatch):
    """Empty query → 422 from Pydantic, before we even check MCP."""
    reset_mcp()
    r = client.post("/search/brave", json={"query": ""})
    assert r.status_code == 422


def test_brave_search_count_bounds(client, monkeypatch):
    """count out of [1, 20] → 422."""
    reset_mcp()
    r = client.post("/search/brave", json={"query": "x", "count": 999})
    assert r.status_code == 422


def test_brave_search_drops_empty_results(client, monkeypatch):
    """Native rows with empty title or url are filtered out — callers
    shouldn't see a row of all-empty fields."""
    reset_mcp()
    fake_reg = MCPClientRegistry()
    fake_reg._clients["brave_search"] = _FakeMCPClient(
        call_result=_brave_web_payload([
            {"title": "Good", "url": "https://g"},
            {"title": "", "url": "https://empty"},
            {"title": "NoUrl", "url": ""},
        ]),
    )
    monkeypatch.setattr(mcp_mod, "get_mcp", lambda: fake_reg)
    monkeypatch.setattr(sb_mod, "get_mcp", lambda: fake_reg)

    r = client.post("/search/brave", json={"query": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["results"][0]["title"] == "Good"
