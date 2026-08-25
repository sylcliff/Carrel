"""Tiny SOCKS5→HTTP CONNECT bridge for stdio MCP servers.

The user's environment routes outbound traffic through a SOCKS5 proxy
(``$all_proxy``, e.g. ``socks5h://127.0.0.1:7892``). Python's
``requests`` / ``urllib`` and Node's older ``http`` module follow
``HTTPS_PROXY`` and have SOCKS-aware transports; Node's modern
``undici.fetch`` (used by ``@brave/brave-search-mcp-server`` and most
current MCP servers) only honors **HTTP CONNECT** proxies, not SOCKS5
— it throws ``TypeError: fetch failed`` instead of routing through
``$all_proxy``.

This script is a ~60-line stdlib-only HTTP CONNECT server that, for
each incoming request, opens a SOCKS5 connection to the upstream and
pipes bytes both ways. Pointing
``HTTPS_PROXY=http://127.0.0.1:7893`` at it from a stdio MCP
subprocess gives that subprocess the same outbound reach the shell
has.

Run:
    python -m tests.socks_http_bridge --listen 127.0.0.1:7893 \\
        --socks 127.0.0.1:7892

Limitations:
- IPv4 + domain CONNECT only (no IPv6 literals, no BIND/UDP_ASSOC).
- No auth. The local SOCKS5 proxy is expected to be authless.
- No keep-alive: a fresh SOCKS5 connection is opened per CONNECT
  request. Brave's web search is plain HTTP/1.1, so this is fine.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import struct

logger = logging.getLogger("socks-http-bridge")


async def _socks5_connect(
    host: str,
    port: int,
    *,
    socks_host: str,
    socks_port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a SOCKS5 CONNECT to ``host:port`` via ``socks_host:socks_port``.

    SOCKS5 protocol reference: RFC 1928. We speak the no-auth method
    (0x00) only. Domain names are encoded as ATYP 0x03; IPv4 as 0x01.
    Returns the live ``(reader, writer)`` pair — the caller is
    responsible for closing it via ``writer.close()`` when done.
    """
    reader, writer = await asyncio.open_connection(socks_host, socks_port)

    # Greeting: VER=5, NMETHODS=1, METHODS=[0x00 (no auth)]
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    resp = await reader.readexactly(2)
    if resp[0] != 0x05 or resp[1] != 0x00:
        writer.close()
        raise RuntimeError(
            f"SOCKS5 server rejected no-auth method: {resp!r}"
        )

    # CONNECT request: VER=5, CMD=1 (connect), RSV=0, ATYP=0x03 (domain)
    try:
        packed_ip = socket.inet_aton(host)
        atyp = b"\x01" + packed_ip
    except OSError:
        encoded = host.encode("idna")
        atyp = bytes([0x03, len(encoded)]) + encoded
    writer.write(b"\x05\x01\x00" + atyp + struct.pack("!H", port))
    await writer.drain()
    head = await reader.readexactly(4)
    if head[0] != 0x05 or head[1] != 0x00:
        writer.close()
        raise RuntimeError(
            f"SOCKS5 CONNECT to {host}:{port} failed: code={head[1]:#x}"
        )
    # Skip the BND.ADDR/BND.PORT — we don't care which bind address the
    # server reports.
    atyp_reply = head[3]
    if atyp_reply == 0x01:  # IPv4
        await reader.readexactly(4 + 2)
    elif atyp_reply == 0x03:  # domain
        dlen = (await reader.readexactly(1))[0]
        await reader.readexactly(dlen + 2)
    elif atyp_reply == 0x04:  # IPv6
        await reader.readexactly(16 + 2)

    return reader, writer


async def _pipe_both(
    cr: asyncio.StreamReader,
    cw: asyncio.StreamWriter,
    ur: asyncio.StreamReader,
    uw: asyncio.StreamWriter,
) -> None:
    """Forward bytes in both directions until either side closes."""

    async def one_way(
        src: asyncio.StreamReader, dst: asyncio.StreamWriter, name: str
    ) -> None:
        try:
            while True:
                data = await src.read(8192)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except Exception:  # noqa: BLE001
            logger.debug("pipe %s→%s ended with error", name, "dst", exc_info=True)
        finally:
            try:
                dst.close()
            except Exception:  # pragma: no cover
                pass

    await asyncio.gather(
        one_way(cr, uw, "client"),
        one_way(ur, cw, "upstream"),
    )


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    socks_host: str,
    socks_port: int,
) -> None:
    """Serve one HTTP CONNECT request, then pipe bytes both ways."""
    try:
        request_line = await client_reader.readline()
        if not request_line:
            return
        try:
            parts = request_line.decode("latin-1").strip().split(" ")
        except UnicodeDecodeError:
            client_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await client_writer.drain()
            return
        if len(parts) != 3 or parts[0] != "CONNECT":
            client_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await client_writer.drain()
            return
        host, _, port_s = parts[1].rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            client_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await client_writer.drain()
            return

        # Drain remaining headers up to the empty line.
        while True:
            line = await client_reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break

        try:
            upstream_reader, upstream_writer = await _socks5_connect(
                host,
                port,
                socks_host=socks_host,
                socks_port=socks_port,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "SOCKS5 connect to %s:%d failed: %s", host, port, e
            )
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            client_writer.close()
            return

        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()

        try:
            await _pipe_both(
                client_reader, client_writer, upstream_reader, upstream_writer
            )
        finally:
            try:
                upstream_writer.close()
            except Exception:  # pragma: no cover
                pass
    except Exception:  # noqa: BLE001
        logger.exception("error handling CONNECT")
        try:
            client_writer.close()
        except Exception:  # pragma: no cover
            pass


async def main(args: argparse.Namespace) -> None:
    listen_host, _, listen_port_s = args.listen.partition(":")
    listen_port = int(listen_port_s)
    socks_host, _, socks_port_s = args.socks.partition(":")
    socks_port = int(socks_port_s)

    server = await asyncio.start_server(
        lambda r, w: _handle_client(
            r, w, socks_host=socks_host, socks_port=socks_port
        ),
        host=listen_host,
        port=listen_port,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logger.info(
        "SOCKS5→HTTP bridge listening on %s → socks %s:%d",
        addrs,
        socks_host,
        socks_port,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--listen", default="127.0.0.1:7893")
    p.add_argument("--socks", default="127.0.0.1:7892")
    p.add_argument("-v", "--verbose", action="store_true")
    parsed = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if parsed.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main(parsed))
    except KeyboardInterrupt:
        pass
