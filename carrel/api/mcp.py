"""MCP integration endpoints.

  * ``GET /mcp/health`` — which servers are configured, which are running,
    and any startup error from the registry.
  * ``GET /mcp/tools``  — every tool exposed by every running server, with
    its name, description, and JSON Schema for arguments. Useful for the
    frontend to render a "what can I call?" inspector, and for debugging
    the bridge after a config change.

Both routes are read-only and never raise. If MCP is disabled or not yet
booted, they return empty / zero-state rather than 503 — these endpoints
exist precisely so the UI can show *why* something is offline.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from carrel.config import CarrelYAML, EnvSettings
from carrel.mcp import MCPClient, get_mcp
from carrel.schemas import MCPHealthResponse, MCPServerHealth, MCPToolInfo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mcp", tags=["mcp"])


def _get_app_config() -> tuple[CarrelYAML, EnvSettings]:
    from carrel.main import app_config, app_env  # noqa: PLC0415

    return app_config, app_env


def _server_health(
    name: str, client: MCPClient | None, enabled: bool
) -> MCPServerHealth:
    if client is None:
        return MCPServerHealth(
            name=name,
            enabled=enabled,
            running=False,
            tool_count=0,
            last_error=None,
        )
    return MCPServerHealth(
        name=name,
        enabled=enabled,
        running=client.is_running,
        tool_count=len(client.tools),
        last_error=client.last_error,
    )


@router.get("/health", response_model=MCPHealthResponse)
def mcp_health() -> MCPHealthResponse:
    cfg, env = _get_app_config()
    enabled = bool(cfg.mcp.enabled and env.mcp_enabled)
    reg = get_mcp()
    if reg is None:
        return MCPHealthResponse(enabled=enabled, servers=[], error=None)

    # Walk every server Carrel *intended* to start, even if it failed at
    # boot, so the UI can flag a server that's "enabled but not running".
    servers: list[MCPServerHealth] = []
    for name, server_cfg in cfg.mcp.servers.items():
        if not server_cfg.enabled:
            servers.append(
                MCPServerHealth(
                    name=name,
                    enabled=False,
                    running=False,
                    tool_count=0,
                    last_error=None,
                )
            )
            continue
        servers.append(_server_health(name, reg.get(name), enabled=True))

    return MCPHealthResponse(enabled=enabled, servers=servers, error=None)


@router.get("/tools", response_model=list[MCPToolInfo])
def mcp_tools() -> list[MCPToolInfo]:
    """Flatten every tool exposed by every running MCP server."""
    reg = get_mcp()
    if reg is None:
        return []
    out: list[MCPToolInfo] = []
    for client in reg.servers():
        for tool in client.tools:
            # mcp Tool exposes input_schema as a dict; defensive default
            # in case an SDK upgrade ever returns None.
            schema = (
                getattr(tool, "inputSchema", None)
                or getattr(tool, "input_schema", {})
                or {}
            )
            out.append(
                MCPToolInfo(
                    server=client.name,
                    name=tool.name,
                    description=tool.description,
                    input_schema=dict(schema),
                )
            )
    return out
