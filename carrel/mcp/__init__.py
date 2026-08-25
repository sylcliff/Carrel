"""MCP (Model Context Protocol) integration.

Long-lived stdio connections to MCP servers. One subprocess per server,
shared across requests, torn down on app shutdown.

The module-level singleton (``_registry``) mirrors the shape used by
``carrel.scheduler``: idempotent ``start_mcp`` / ``stop_mcp`` and a
``get_mcp`` accessor for routers.
"""
from __future__ import annotations

from carrel.mcp.client import (
    MCPClient,
    MCPClientRegistry,
    get_mcp,
    reset_mcp,
    start_mcp,
    stop_mcp,
)
from carrel.mcp.errors import MCPError, MCPUnavailable

__all__ = [
    "MCPClient",
    "MCPClientRegistry",
    "MCPError",
    "MCPUnavailable",
    "get_mcp",
    "reset_mcp",
    "start_mcp",
    "stop_mcp",
]
