"""Long-lived MCP client (v1 stdio pattern).

One :class:`MCPClient` per server holds a single subprocess and a
:func:`mcp.ClientSession` for the lifetime of the Carrel process. Callers
acquire a per-client ``asyncio.Lock`` before issuing ``call_tool`` because
``ClientSession`` is not safe for concurrent awaits.

The :class:`MCPClientRegistry` aggregates multiple clients; the module-level
singleton (``start_mcp`` / ``stop_mcp`` / ``get_mcp``) is the FastAPI
lifecycle hook. Mirrors the shape of :mod:`carrel.scheduler`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp.client.stdio import stdio_client

from carrel.config import CarrelYAML, EnvSettings, MCPServerConfig
from carrel.mcp.errors import MCPError, MCPUnavailable
from carrel.mcp.socks_bridge import SocksHttpBridge
from mcp import ClientSession, StdioServerParameters

logger = logging.getLogger("carrel.mcp")


class MCPClient:
    """One persistent connection to one MCP server.

    v1 SDK pattern: ``stdio_client(params)`` yields (read, write); wrap a
    :class:`ClientSession` around them and call ``initialize()`` once. We
    enter both context managers manually via :class:`AsyncExitStack` so the
    connection stays alive until :meth:`stop` is called.
    """

    def __init__(
        self,
        name: str,
        params: StdioServerParameters,
        *,
        tool_call_timeout: float = 30.0,
        startup_timeout: float = 15.0,
    ) -> None:
        self.name = name
        self._params = params
        self._tool_call_timeout = tool_call_timeout
        self._startup_timeout = startup_timeout
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[Any] = []
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        try:
            async def _open() -> Any:
                read, write = await self._stack.enter_async_context(  # type: ignore[union-attr]
                    stdio_client(self._params)
                )
                session = await self._stack.enter_async_context(  # type: ignore[union-attr]
                    ClientSession(read, write)
                )
                await asyncio.wait_for(session.initialize(), timeout=self._startup_timeout)
                return session

            session: ClientSession = await asyncio.wait_for(  # type: ignore[assignment]
                _open(), timeout=self._startup_timeout,
            )
            self._session = session
            result = await session.list_tools()
            self._tools = result.tools
            self.last_error = None
            logger.info(
                "MCP server %r started with %d tools: %s",
                self.name,
                len(self._tools),
                ", ".join(t.name for t in self._tools),
            )
        except Exception as e:
            logger.exception("failed to start MCP server %r", self.name)
            if self._stack is not None:
                try:
                    await self._stack.aclose()
                except Exception:  # pragma: no cover - best-effort cleanup
                    logger.exception("error cleaning up %r after failed start", self.name)
            self._stack = None
            self._session = None
            self._tools = []
            self.last_error = str(e)
            raise MCPError(f"failed to start MCP server {self.name!r}: {e}") from e

    async def stop(self) -> None:
        if self._stack is None:
            return
        try:
            await self._stack.aclose()
        except Exception:  # pragma: no cover - shutdown best-effort
            logger.exception("error closing MCP server %r", self.name)
        finally:
            self._stack = None
            self._session = None
            self._tools = []

    @property
    def tools(self) -> list[Any]:
        return list(self._tools)

    @property
    def is_running(self) -> bool:
        return self._session is not None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Forward ``name(arguments)`` to the server under a single lock.

        A :class:`MCPUnavailable` is raised when the client is not running
        (caller should ``HTTPException(503)``); a :class:`MCPError` wraps
        timeouts and other transport failures (``HTTPException(502)``).
        """
        if self._session is None:
            raise MCPUnavailable(f"MCP server {self.name!r} is not running")
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    self._session.call_tool(name, arguments),
                    timeout=self._tool_call_timeout,
                )
            except TimeoutError as e:
                raise MCPError(
                    f"MCP tool {self.name!r}.{name} timed out after "
                    f"{self._tool_call_timeout}s"
                ) from e


class MCPClientRegistry:
    """Holds the :class:`MCPClient` for every started server."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._bridge: SocksHttpBridge | None = None

    async def start_all(
        self,
        configs: list[tuple[str, MCPServerConfig, StdioServerParameters]],
        *,
        default_startup_timeout: float = 15.0,
        default_tool_call_timeout: float = 30.0,
    ) -> None:
        # Spin up a single SOCKS→HTTP bridge if any enabled server's
        # env references the sentinel. Done once, shared, and torn down
        # in :meth:`stop_all`.
        bridge = SocksHttpBridge.maybe_start()
        self._bridge = bridge

        for name, cfg, params in configs:
            if not cfg.enabled:
                logger.info("MCP server %r disabled by config; skipping", name)
                continue
            if bridge is not None:
                # Rewrite sentinel values in the subprocess env to point
                # at the bridge's real URL. params.env is read-only inside
                # StdioServerParameters, so build a fresh one.
                rewritten = bridge.rewrite_env(params.env or {})
                params = StdioServerParameters(
                    command=params.command,
                    args=params.args,
                    env=rewritten,
                    cwd=params.cwd,
                )
            client = MCPClient(
                name,
                params,
                tool_call_timeout=(
                    cfg.tool_call_timeout_seconds
                    if cfg.tool_call_timeout_seconds is not None
                    else default_tool_call_timeout
                ),
                startup_timeout=(
                    cfg.startup_timeout_seconds
                    if cfg.startup_timeout_seconds is not None
                    else default_startup_timeout
                ),
            )
            try:
                await client.start()
            except MCPError:
                # Already logged + recorded in client.last_error; the registry
                # just doesn't track it. /mcp/health will report it as missing.
                continue
            self._clients[name] = client

    async def stop_all(self) -> None:
        for name, client in list(self._clients.items()):
            try:
                await client.stop()
            except Exception:  # pragma: no cover - shutdown best-effort
                logger.exception("error stopping MCP server %r", name)
        self._clients.clear()
        if self._bridge is not None:
            try:
                self._bridge.stop()
            except Exception:  # pragma: no cover
                logger.exception("error stopping SOCKS bridge")
            self._bridge = None

    def get(self, name: str) -> MCPClient | None:
        return self._clients.get(name)

    def servers(self) -> list[MCPClient]:
        return list(self._clients.values())


# ---------------------------------------------------------------------------
# Module-level singleton — wired by FastAPI lifespan
# ---------------------------------------------------------------------------

_registry: MCPClientRegistry | None = None


def _build_params(
    name: str,
    cfg: MCPServerConfig,
    env: EnvSettings,
) -> StdioServerParameters:
    """Construct the subprocess launch params for one server.

    Subprocess env layering (each step overrides the previous):

    1. Carrel's own ``os.environ`` (so ``PATH``, ``NODE_PATH``, proxy vars,
       and locale propagate).
    2. Per-server ``env: dict`` from YAML (rarely used; for non-secret flags).
    3. Specific secrets from :class:`EnvSettings`, dispatched by server name
       — currently only ``brave_api_key`` → ``BRAVE_API_KEY`` for the
       ``brave_search`` server.
    """
    proc_env = dict(os.environ)
    proc_env.update(cfg.env)
    if name == "brave_search" and env.brave_api_key:
        proc_env["BRAVE_API_KEY"] = env.brave_api_key
    return StdioServerParameters(
        command=cfg.command,
        args=cfg.args,
        env=proc_env,
    )


async def start_mcp(
    cfg: CarrelYAML,
    env: EnvSettings,
) -> MCPClientRegistry | None:
    """Boot every enabled MCP server. Idempotent.

    Returns the registry on success, ``None`` if MCP is disabled or no
    servers are configured. Failures to start individual servers are
    logged and recorded via :attr:`MCPClient.last_error`; the registry
    only holds the ones that came up.
    """
    global _registry
    if _registry is not None:
        return _registry
    if not cfg.mcp.enabled or not env.mcp_enabled:
        logger.info("MCP integration disabled (cfg.mcp.enabled=%s, env.mcp_enabled=%s)",
                    cfg.mcp.enabled, env.mcp_enabled)
        return None
    items: list[tuple[str, MCPServerConfig, StdioServerParameters]] = []
    for name, server_cfg in cfg.mcp.servers.items():
        if not server_cfg.enabled:
            continue
        items.append((name, server_cfg, _build_params(name, server_cfg, env)))
    if not items:
        logger.info("MCP enabled but no servers configured")
        return None
    reg = MCPClientRegistry()
    await reg.start_all(
        items,
        default_startup_timeout=cfg.mcp.startup_timeout_seconds,
        default_tool_call_timeout=cfg.mcp.tool_call_timeout_seconds,
    )
    _registry = reg
    return reg


async def stop_mcp() -> None:
    global _registry
    if _registry is None:
        return
    await _registry.stop_all()
    _registry = None


def get_mcp() -> MCPClientRegistry | None:
    """Return the live registry or ``None`` if MCP is disabled / not started."""
    return _registry


def reset_mcp() -> None:
    """Test-only: drop the singleton without touching subprocesses."""
    global _registry
    _registry = None
