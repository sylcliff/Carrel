"""MCP (Model Context Protocol) integration.

Long-lived stdio connections to MCP servers. One subprocess per server,
shared across requests, torn down on app shutdown.

The module-level singleton (``_registry``) mirrors the shape used by
``carrel.scheduler``: idempotent ``start_mcp`` / ``stop_mcp`` and a
``get_mcp`` accessor for routers.
"""
from __future__ import annotations

from carrel.mcp.builtin_tools import (
    BUILTIN_SERVER_NAME,
    builtin_dispatch_map,
    collect_builtin_tools,
    dispatch_builtin,
)
from carrel.mcp.client import (
    MCPClient,
    MCPClientRegistry,
    get_mcp,
    reset_mcp,
    start_mcp,
    stop_mcp,
)
from carrel.mcp.errors import MCPError, MCPUnavailable
from carrel.mcp.tools import (
    collect_tools,
    dispatch_tool_call,
    litellm_arguments,
    mcp_tool_to_litellm,
    parse_litellm_name,
)

__all__ = [
    "BUILTIN_SERVER_NAME",
    "MCPClient",
    "MCPClientRegistry",
    "MCPError",
    "MCPUnavailable",
    "builtin_dispatch_map",
    "collect_builtin_tools",
    "collect_tools",
    "dispatch_builtin",
    "dispatch_tool_call",
    "get_mcp",
    "litellm_arguments",
    "mcp_tool_to_litellm",
    "parse_litellm_name",
    "reset_mcp",
    "start_mcp",
    "stop_mcp",
]
