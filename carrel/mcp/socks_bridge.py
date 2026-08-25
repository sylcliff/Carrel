"""Managed SOCKS5→HTTP CONNECT bridge sidecar.

Spawns :mod:`tests.socks_http_bridge` as a child process and returns
its ``http://127.0.0.1:<port>`` URL. Callers substitute that URL into
the env of stdio MCP servers that need HTTP-CONNECT-only egress (e.g.
Node's ``undici`` inside ``@brave/brave-search-mcp-server``).

The bridge is shared across all MCP servers in one Carrel process
because it's stateless and cheap; any number of TCP connections can
be funneled through it. The port is picked at start time so we never
collide with anything on the host.

If a real ``HTTPS_PROXY`` / ``HTTP_PROXY`` is already set in the
environment (a working HTTP CONNECT proxy), :meth:`SocksHttpBridge.maybe_start`
returns ``None`` and no sidecar is launched — the env var is left as-is.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Sentinel substituted into MCP server envs. Replaced at registry
# start time with the bridge's real URL.
BRIDGE_SENTINEL = "__CARREL_SOCKS_BRIDGE__"

# Local SOCKS5 proxy address. On this machine $all_proxy points at
# 127.0.0.1:7892 (FlashFoxL). The bridge forwards CONNECT requests
# through it. Override with ``CARREL_SOCKS_UPSTREAM=host:port`` if
# your environment differs.
DEFAULT_SOCKS_UPSTREAM = "127.0.0.1:7892"


def _pick_free_port() -> int:
    """Bind to port 0, return the OS-assigned port, close the socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SocksHttpBridge:
    """One long-lived bridge subprocess."""

    def __init__(self, *, socks_upstream: str, python: str | None = None) -> None:
        self._socks_upstream = socks_upstream
        self._python = python or sys.executable
        self._proc: subprocess.Popen[bytes] | None = None
        self._port: int | None = None

    @classmethod
    def maybe_start(cls) -> "SocksHttpBridge | None":
        """Start a bridge only when one is actually needed.

        Returns ``None`` if the SOCKS5 upstream isn't reachable (refuse
        to start a broken sidecar — let the upstream MCP server fail
        with a clear error instead). The presence of a pre-existing
        ``HTTPS_PROXY`` in the environment does NOT short-circuit the
        bridge: the env var often points at a SOCKS proxy (which
        undici can't use anyway), and the YAML sentinel is the real
        signal that the caller wants a bridge.
        """
        upstream = os.environ.get("CARREL_SOCKS_UPSTREAM", DEFAULT_SOCKS_UPSTREAM)
        host, _, port_s = upstream.partition(":")
        try:
            port = int(port_s)
            with socket.create_connection((host, port), timeout=2.0):
                pass
        except OSError as e:
            logger.warning(
                "SOCKS5 upstream %s unreachable (%s); bridge not started", upstream, e
            )
            return None

        return cls(socks_upstream=upstream)._start()

    def _start(self) -> "SocksHttpBridge":
        port = _pick_free_port()
        bridge_module = self._bridge_module_path()
        if bridge_module is None:
            raise RuntimeError(
                "cannot locate tests/socks_http_bridge.py relative to the project"
            )
        logger.info(
            "starting SOCKS5→HTTP bridge on 127.0.0.1:%d (upstream %s)",
            port,
            self._socks_upstream,
        )
        self._proc = subprocess.Popen(
            [
                self._python,
                bridge_module,
                "--listen",
                f"127.0.0.1:{port}",
                "--socks",
                self._socks_upstream,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._port = port
        return self

    @staticmethod
    def _bridge_module_path() -> str | None:
        """Return the absolute path of ``tests/socks_http_bridge.py``.

        Searches the project root (``cwd``) first, then the directory
        two levels up — covers the common Carrel layout and editable
        installs.
        """
        cwd = Path(os.getcwd()).resolve()
        for base in (cwd, *cwd.parents):
            candidate = base / "tests" / "socks_http_bridge.py"
            if candidate.is_file():
                return str(candidate)
        return None

    @property
    def url(self) -> str:
        if self._port is None:
            raise RuntimeError("bridge not started")
        return f"http://127.0.0.1:{self._port}"

    def rewrite_env(self, env: dict[str, str]) -> dict[str, str]:
        """Return ``env`` with any :data:`BRIDGE_SENTINEL` value replaced.

        Leaves env untouched when no sentinel is present (the MCP
        server either doesn't need a bridge or already has a real
        proxy configured).
        """
        if not any(v == BRIDGE_SENTINEL for v in env.values()):
            return env
        out = dict(env)
        url = self.url
        for key, value in list(out.items()):
            if value == BRIDGE_SENTINEL:
                out[key] = url
        return out

    def stop(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                logger.warning("bridge did not exit on SIGTERM; killing")
                proc.kill()
                proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            logger.exception("error stopping SOCKS bridge")
        finally:
            self._port = None

    def __enter__(self) -> "SocksHttpBridge":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
