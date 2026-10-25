import pytest

from price_scanner.blocking import classify
from price_scanner.fetcher import Fetcher
from price_scanner.service import PriceScanner, build_search_url
from price_scanner.store import Store
from tests.conftest import fixture_html


class FakeFetcher(Fetcher):
    """Fetcher whose network layer returns canned pages and counts requests."""

    def __init__(self, config, pages, store=None, cache_ttl_seconds=300):
        super().__init__(config, cache_ttl_seconds=cache_ttl_seconds, store=store)
        self.pages = pages  # url substring -> (status, html)
        self.requests = []

    async def _fetch_http(self, url, timeout):
        self.requests.append(url)
        for key, (status, html) in self.pages.items():
            if key in url:
                return status, url, html
        return 404, url, "<html></html>"

    async def _fetch_browser(self, source, url, wait_for, timeout):
        return await self._fetch_http(url, timeout)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr("price_scanner.fetcher.asyncio.sleep", _sleep)


@pytest.fixture
def make(tmp_path):
    """A scanner over a fresh temporary store (v2 refuses to search or fetch without one)."""
    stores = []

    def _make(config, pages, profile_dir=None):
        store = Store(tmp_path / f"store-{len(stores)}.db")
        stores.append(store)
        fetcher = FakeFetcher(config, pages, store)
        return PriceScanner(config, fetcher, profile_dir=profile_dir, store=store, store_path=store.path), fetcher

    yield _make
    for store in stores:
        store.close()


def test_build_search_url(config):
    source = config.sources["example"]
    url, applied = build_search_url(source, "red bike", "newest")
    assert url == "https://market.example/search?q=red%20bike&order=new" and applied
    url, applied = build_search_url(source, "red bike", "price_asc")
    assert url == "https://market.example/search?q=red%20bike" and not applied


async def test_search_ok(config, make):
    scanner, fetcher = make(config, {"/search": (200, fixture_html("search_cards.html"))})
    out = await scanner.search_marketplace("example", "bike", limit=2)
    assert out["status"] == "ok" and out["count"] == 2
    assert out["listings"][0]["site"] == "example"
    assert len(fetcher.requests) == 1
    again = await scanner.search_marketplace("example", "bike", limit=2)
    assert again["from_cache"] is True and again["status"] == "cached" and len(fetcher.requests) == 1


async def test_blocked_reports_blocked_and_cools_down(config, make):
    scanner, fetcher = make(config, {"/search": (200, fixture_html("blocked_datadome.html"))})
    out = await scanner.search_marketplace("example", "bike")
    assert out["status"] == "blocked" and out["listings"] == [] and out["count"] == 0
    assert out["retry_after_seconds"] == 3600
    # During the cooldown nothing is sent to the site.
    out2 = await scanner.search_marketplace("example", "other query")
    assert out2["status"] == "blocked" and len(fetcher.requests) == 1
    health = await scanner.health(probe=True)
    entry = next(s for s in health["sources"] if s["key"] == "example")
    assert entry["state"] == "blocked" and len(fetcher.requests) == 1


async def test_403_is_blocked(config, make):
    scanner, _ = make(config, {"/search": (403, "<html></html>")})
    out = await scanner.search_marketplace("example", "bike")
    assert out["status"] == "blocked" and out["reason"] == "http_403"


async def test_unknown_disabled_gated(config, make):
    scanner, fetcher = make(config, {})
    assert (await scanner.search_marketplace("nope", "x"))["status"] == "error"
    assert (await scanner.search_marketplace("off", "x"))["status"] == "disabled"
    assert (await scanner.search_marketplace("gated", "x"))["status"] == "login-required"
    assert (await scanner.search_marketplace("example", ""))["status"] == "error"
    assert fetcher.requests == []


async def test_fetch_listing(config, make):
    scanner, _ = make(config, {"old-radio": (200, fixture_html("listing_jsonld_nodate.html"))})
    out = await scanner.fetch_listing("https://shop.example/item/old-radio-1003.html")
    assert out["status"] == "ok"
    assert out["listing"]["posted_date"] is None and out["listing"]["date_reliable"] is False


async def test_fetch_listing_rejects_unconfigured_hosts(config, make):
    scanner, fetcher = make(config, {})
    assert (await scanner.fetch_listing("https://elsewhere.example/a"))["status"] == "error"
    assert (await scanner.fetch_listing("file:///etc/passwd"))["status"] == "error"
    assert (await scanner.fetch_listing("https://off.example/a"))["status"] == "disabled"
    assert fetcher.requests == []


async def test_list_sources_and_health_without_probe_send_nothing(config, make):
    scanner, fetcher = make(config, {})
    sources = scanner.list_sources()
    keys = {s["key"]: s for s in sources["sources"]}
    assert keys["example"]["search"] is True and keys["example"]["dates_reliable"] is True
    assert keys["gated"]["login_required"] is True
    health = await scanner.health(probe=False)
    states = {s["key"]: s["state"] for s in health["sources"]}
    assert states == {"example": "unknown", "gated": "login-required", "off": "disabled"}
    assert fetcher.requests == []


async def test_health_probe_hits_home_page_only_once(config, make):
    scanner, fetcher = make(config, {"market.example/": (200, "<html><title>Home</title></html>")})
    health = await scanner.health(probe=True)
    states = {s["key"]: s["state"] for s in health["sources"]}
    assert states["example"] == "reachable"
    assert fetcher.requests == ["https://market.example/"]
    await scanner.health(probe=True)
    assert fetcher.requests == ["https://market.example/"]  # reused, not re-probed


def test_classify_used_by_fetcher_is_strict_about_markers():
    assert classify(200, "https://x.example/", "<script src='https://js.example/datadome-tag.js'></script>").state == "ok"
