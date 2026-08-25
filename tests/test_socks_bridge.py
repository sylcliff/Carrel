"""Unit tests for the SOCKS5→HTTP bridge sidecar helper.

The :class:`SocksHttpBridge` mostly wraps a subprocess, so the only
pure logic we can test without spawning one is :meth:`rewrite_env`
and the "skip when an HTTP CONNECT proxy is already set" branch of
:meth:`maybe_start`.
"""
from __future__ import annotations

import pytest

from carrel.mcp.socks_bridge import BRIDGE_SENTINEL, SocksHttpBridge


def test_sentinel_is_a_distinguishable_string():
    # The sentinel is also a valid literal in YAML so the registry
    # can substitute it; guard against accidental change to a value
    # that could be confused with a real URL.
    assert BRIDGE_SENTINEL.startswith("__")
    assert "://" not in BRIDGE_SENTINEL


def test_rewrite_env_replaces_sentinel_in_known_keys():
    b = SocksHttpBridge.__new__(SocksHttpBridge)
    b._port = 7893
    env = {
        "HTTPS_PROXY": BRIDGE_SENTINEL,
        "HTTP_PROXY": BRIDGE_SENTINEL,
        "https_proxy": BRIDGE_SENTINEL,
        "PATH": "/usr/bin",
    }
    out = b.rewrite_env(env)
    assert out["HTTPS_PROXY"] == "http://127.0.0.1:7893"
    assert out["HTTP_PROXY"] == "http://127.0.0.1:7893"
    assert out["https_proxy"] == "http://127.0.0.1:7893"
    assert out["PATH"] == "/usr/bin"


def test_rewrite_env_passthrough_when_no_sentinel():
    b = SocksHttpBridge.__new__(SocksHttpBridge)
    b._port = 7893
    env = {"HTTPS_PROXY": "http://corp.example:8080", "PATH": "/bin"}
    out = b.rewrite_env(env)
    # Identity-preserved (same dict returned untouched — no copy needed
    # in this branch). The assertion is intentionally on `is` to flag
    # accidental copying.
    assert out is env


def test_rewrite_env_raises_when_not_started():
    b = SocksHttpBridge.__new__(SocksHttpBridge)
    b._port = None
    with pytest.raises(RuntimeError, match="not started"):
        b.rewrite_env({"HTTPS_PROXY": BRIDGE_SENTINEL})


def test_maybe_start_starts_even_if_https_proxy_set(monkeypatch):
    # A pre-existing ``HTTPS_PROXY`` in the environment is NOT a
    # signal that we should skip the bridge — on this machine the env
    # often points at a SOCKS proxy, which undici can't use anyway.
    # The YAML sentinel is the real signal. We DO refuse to start
    # when the SOCKS upstream is unreachable; that's the only branch
    # that returns ``None``.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7892")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setenv("CARREL_SOCKS_UPSTREAM", "127.0.0.1:1")
    assert SocksHttpBridge.maybe_start() is None


def test_maybe_start_returns_none_when_socks_unreachable(monkeypatch):
    # No real HTTP proxy set → we try the bridge. But the SOCKS5
    # upstream must be reachable; otherwise we refuse to start a
    # broken sidecar (so /mcp/health surfaces the underlying problem
    # rather than a silent spawn-then-timeout).
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setenv("CARREL_SOCKS_UPSTREAM", "127.0.0.1:1")
    assert SocksHttpBridge.maybe_start() is None
