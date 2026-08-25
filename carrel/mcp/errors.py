"""Exceptions for the MCP integration.

Patterned on ``carrel.sources.mineru_client.MinerUError``: the client raises
a specific exception and the API layer maps it to an ``HTTPException`` so
the FastAPI default handler never sees the raw MCP error shape.
"""
from __future__ import annotations


class MCPError(Exception):
    """Generic MCP failure (subprocess crash, protocol error, timeout)."""


class MCPUnavailable(MCPError):
    """The MCP server is configured but its subprocess is not running.

    Distinct from :class:`MCPError` so the API layer can return 503
    (service unavailable) instead of 502 (bad gateway) — the failure is
    local configuration, not an upstream error.
    """
