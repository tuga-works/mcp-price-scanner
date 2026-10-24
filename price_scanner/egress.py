"""Egress guard: a tiny local CONNECT proxy that only reaches public hosts.

Chromium and the plain-HTTP client send every request through this proxy,
including redirect hops (which Playwright's request routing never sees) and
subresources. For each ``CONNECT host:443`` the guard resolves the host
itself, refuses the tunnel if any resolved address is not public unicast
(loopback, private, link-local, CGNAT/overlay, multicast, reserved), and
otherwise connects to the address it validated, so a DNS answer cannot change
between the check and the connection.

TLS stays end to end between the browser and the site; the guard only relays
bytes, so the browser's own identity is unchanged. Plain-HTTP proxy requests
and ports other than 443 are refused.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Callable

from price_scanner.urlsafety import address_allowed, normalize_host

log = logging.getLogger(__name__)

HEADER_LIMIT = 16 * 1024
CONNECT_TIMEOUT = 15.0


class EgressGuard:
    def __init__(self, *, allowed_ports: frozenset[int] = frozenset({443}),
                 address_check: Callable[[str], bool] = address_allowed) -> None:
        self.allowed_ports = allowed_ports
        self.address_check = address_check
        self._server: asyncio.base_events.Server | None = None
        self.port: int | None = None
        self.refused = 0

    @property
    def proxy_url(self) -> str:
        assert self.port is not None, "guard not started"
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            log.info("egress guard listening on loopback")

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _reply(self, writer: asyncio.StreamWriter, status: str) -> None:
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        try:
            await writer.drain()
        finally:
            writer.close()

    async def _resolve(self, host: str, port: int) -> list[str]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [info[4][0] for info in infos]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=CONNECT_TIMEOUT)
        except Exception:
            writer.close()
            return
        if len(head) > HEADER_LIMIT:
            await self._reply(writer, "431 Request Header Fields Too Large")
            return
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split(" ")
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            # Plain-HTTP proxying is not offered. 405 (not 403) so a refused
            # http:// hop is not mistaken for the site blocking the bridge.
            self.refused += 1
            await self._reply(writer, "405 Method Not Allowed")
            return
        host, _, port_text = parts[1].rpartition(":")
        host = normalize_host(host.strip("[]"))
        try:
            port = int(port_text)
        except ValueError:
            port = -1
        if not host or port not in self.allowed_ports:
            self.refused += 1
            await self._reply(writer, "403 Forbidden")
            return
        try:
            addresses = await asyncio.wait_for(self._resolve(host, port), timeout=CONNECT_TIMEOUT)
        except Exception:
            await self._reply(writer, "502 Bad Gateway")
            return
        if not addresses or not all(self.address_check(a) for a in addresses):
            self.refused += 1
            log.warning("egress guard refused a non-public destination")
            await self._reply(writer, "403 Forbidden")
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], port), timeout=CONNECT_TIMEOUT)
        except Exception:
            await self._reply(writer, "502 Bad Gateway")
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer),
                             return_exceptions=True)

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(65536)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        finally:
            try:
                dst.close()
            except Exception:
                pass
