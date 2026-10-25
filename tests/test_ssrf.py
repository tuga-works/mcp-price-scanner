"""SSRF guards: URL sanitising, host matching, redirect discard, egress guard."""

import asyncio

import pytest

from price_scanner.egress import EgressGuard
from price_scanner.service import PriceScanner
from price_scanner.sources import SourcesConfig
from price_scanner.store import Store
from price_scanner.urlsafety import UnsafeURL, address_allowed, host_matches, sanitize_listing_url
from tests.conftest import EXAMPLE_SOURCES, fixture_html
from tests.test_service import FakeFetcher


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example\\@market.example/item/1",  # backslash + userinfo confusion
        "https://evil.example\\.market.example/item/1",
        "https://user@market.example/item/1",  # userinfo
        "https://market.example@evil.example/item/1",
        "https://user:pass@market.example/item/1",
        "https://market.example/item 1",  # whitespace
        "https://market.example/item\t1",
        "https://market.example/item\x00",  # control character
        "https://127.0.0.1/item/1",  # IP literals
        "https://[::1]/item/1",
        "https://2130706433/item/1",  # decimal IPv4 form
        "https://0x7f.1/item/1",  # hex IPv4 form
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://market.example/x",
        "https://market.example:8080/item/1",  # non-default port
        "https:///item/1",
        "",
        None,
    ],
)
def test_rejected_urls(url):
    with pytest.raises(UnsafeURL):
        sanitize_listing_url(url)


def test_rebuilt_url_drops_fragment_and_trailing_dot_and_upgrades_http():
    assert sanitize_listing_url("HTTPS://Market.Example./item/1?x=2#frag") == "https://market.example/item/1?x=2"
    assert sanitize_listing_url("http://market.example/item/1") == "https://market.example/item/1"
    assert sanitize_listing_url("https://market.example:443/a") == "https://market.example/a"


def test_host_matching():
    domains = ["market.example"]
    assert host_matches("market.example", domains)
    assert host_matches("www.market.example", domains)
    assert host_matches("market.example.", domains)  # trailing dot
    assert host_matches("MARKET.EXAMPLE", domains)
    assert not host_matches("market.example.evil.example", domains)
    assert not host_matches("evilmarket.example", domains)
    assert not host_matches("", domains)


@pytest.mark.parametrize(
    "address, allowed",
    [
        ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.0.10", False),
        ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
        ("::1", False), ("fd00::1", False), ("fe80::1", False), ("::ffff:192.168.0.1", False),
        ("93.184.215.14", True), ("2606:4700::1111", True), ("not-an-ip", False),
    ],
)
def test_address_allowed(address, allowed):
    assert address_allowed(address) is allowed


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr("price_scanner.fetcher.asyncio.sleep", _sleep)


class RedirectingFetcher(FakeFetcher):
    async def _fetch_http(self, url, timeout):
        self.requests.append(url)
        return 200, "https://elsewhere.example/landing", fixture_html("listing_jsonld_nodate.html")


async def test_redirect_off_domain_is_discarded_and_not_cached(tmp_path):
    config = SourcesConfig.model_validate(EXAMPLE_SOURCES)
    store = Store(tmp_path / "ps.db")
    fetcher = RedirectingFetcher(config, {}, store)
    scanner = PriceScanner(config, fetcher, store=store)
    out = await scanner.fetch_listing("https://market.example/item/old-radio-1003.html")
    assert out["status"] == "error" and out["reason"] == "redirected_off_source"
    assert "listing" not in out
    out = await scanner.fetch_listing("https://market.example/item/old-radio-1003.html")
    assert len(fetcher.requests) == 2  # nothing was cached, in memory or in the store
    assert store.status()["responses"] == 0


async def test_fetch_listing_rejects_bypass_without_request(tmp_path):
    config = SourcesConfig.model_validate(EXAMPLE_SOURCES)
    store = Store(tmp_path / "ps.db")
    fetcher = FakeFetcher(config, {}, store)
    scanner = PriceScanner(config, fetcher, store=store)
    for url in ["https://evil.example\\@market.example/x", "https://user@market.example/x",
                "https://127.0.0.1/x", "https://market.example.evil.example/x"]:
        assert (await scanner.fetch_listing(url))["status"] == "error"
    assert fetcher.requests == []
    # A trailing-dot host is normalised and fetched as the clean URL.
    fetcher.pages = {"old-radio": (200, fixture_html("listing_jsonld_nodate.html"))}
    out = await scanner.fetch_listing("https://market.example./item/old-radio-1003.html#x")
    assert out["status"] == "ok" and fetcher.requests == ["https://market.example/item/old-radio-1003.html"]


async def _proxy_request(port, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(1024), timeout=5)
    writer.close()
    return data


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n",
        b"CONNECT localhost:443 HTTP/1.1\r\nHost: localhost:443\r\n\r\n",
        b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
        b"CONNECT 192.168.0.1:443 HTTP/1.1\r\n\r\n",
        b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n",
        b"CONNECT market.example:22 HTTP/1.1\r\n\r\n",  # port not allowed
    ],
)
async def test_egress_guard_refuses(request_bytes):
    guard = EgressGuard()
    await guard.start()
    try:
        reply = await _proxy_request(guard.port, request_bytes)
        assert reply.startswith(b"HTTP/1.1 403")
    finally:
        await guard.close()


async def test_egress_guard_refuses_plain_http_proxying():
    guard = EgressGuard()
    await guard.start()
    try:
        reply = await _proxy_request(guard.port, b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 405")
    finally:
        await guard.close()


async def test_egress_guard_tunnels_allowed_destination():
    async def echo(reader, writer):
        writer.write(await reader.read(100))
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    # Test-only relaxation: allow loopback and the echo port.
    guard = EgressGuard(allowed_ports=frozenset({port}), address_check=lambda a: True)
    await guard.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", guard.port)
        writer.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(4), timeout=5) == b"ping"
        writer.close()
    finally:
        await guard.close()
        upstream.close()
