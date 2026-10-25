"""The persistent store and the as-few-live-requests-as-possible rule.

Every test uses a temporary database file and a fake fetcher that counts
live requests. Nothing touches the network.
"""

import asyncio
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from price_scanner.fetcher import Fetcher
from price_scanner.service import PriceScanner, normalize_query, search_target
from price_scanner.store import Store, StoreError, iso, parse_iso
from tests.conftest import fixture_html

REAL_SLEEP = asyncio.sleep  # captured before any test patches asyncio.sleep

SEARCH = "/search"
LISTING_URL = "https://shop.example/item/old-radio-1003.html"
OK_PAGES = {SEARCH: (200, fixture_html("search_cards.html")),
            "old-radio": (200, fixture_html("listing_jsonld_nodate.html"))}
BLOCK_PAGES = {SEARCH: (200, fixture_html("blocked_datadome.html")), "old-radio": (403, "<html></html>")}


class CountingFetcher(Fetcher):
    """No in-memory page cache (ttl 0): every answer without a request is the store's doing."""

    def __init__(self, config, pages, store=None, *, clock=None, slow=False):
        super().__init__(config, cache_ttl_seconds=0, store=store, clock=clock)
        self.pages = dict(pages)
        self.requests = []
        self.slow = slow

    async def _fetch_http(self, url, timeout):
        self.requests.append(url)
        if self.slow:
            await REAL_SLEEP(0.02)  # a real suspension point, so concurrent callers overlap
        for key, (status, html) in self.pages.items():
            if key in url:
                return status, url, html
        return 404, url, "<html></html>"

    async def _fetch_browser(self, source, url, wait_for, timeout):
        return await self._fetch_http(url, timeout)


class Clock:
    """A controllable UTC clock shared by the store, the fetcher and the scanner."""

    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr("price_scanner.fetcher.asyncio.sleep", _sleep)


def build(config, db_path, pages, clock, *, search_ttl=3600, fetch_ttl=7200, slow=False):
    """One service start: open the store, restore cooldowns, build the scanner."""
    store = Store(db_path, clock=clock)
    fetcher = CountingFetcher(config, pages, store, clock=clock, slow=slow)
    fetcher.restore_cooldowns()
    scanner = PriceScanner(config, fetcher, store=store, store_path=db_path, clock=clock,
                           search_ttl_seconds=search_ttl, fetch_ttl_seconds=fetch_ttl)
    return scanner, fetcher, store


def plus(timestamp, seconds):
    return iso(parse_iso(timestamp) + timedelta(seconds=seconds))


async def test_second_identical_call_is_served_from_sqlite_without_a_live_request(config, tmp_path):
    clock, db = Clock(), tmp_path / "ps.db"
    scanner, fetcher, store = build(config, db, OK_PAGES, clock)
    first = await scanner.search_marketplace("example", "bike", limit=2)
    assert first["status"] == "ok" and first["from_cache"] is False and first["count"] == 2
    assert first["cached_at"] is None and first["age_seconds"] == 0
    assert first["next_refresh_at"] == plus(first["fetched_at"], 3600)
    assert first["cooldown_until"] is None and first["cooldown_remaining_seconds"] == 0
    assert first["cooldown_reason"] is None and first["target"] == "newest:bike"
    assert len(fetcher.requests) == 1

    clock.advance(120)
    second = await scanner.search_marketplace("example", "bike", limit=2)
    assert second["status"] == "cached" and second["from_cache"] is True
    assert second["cached_at"] == first["fetched_at"] and second["age_seconds"] == 120
    assert second["next_refresh_at"] == plus(second["cached_at"], 3600)
    assert second["listings"] == first["listings"] and second["count"] == 2
    assert second["cooldown_until"] is None and second["cooldown_remaining_seconds"] == 0
    assert len(fetcher.requests) == 1

    # The whole page is stored, exactly as it was returned (before the caller's limit).
    row = store.get_response("example", "search", "newest:bike")
    assert row.response["status"] == "ok" and row.response["count"] == 3 and row.block_state == "reachable"
    assert row.response["listings"][:2] == first["listings"]

    # A restart: empty memory, same database file.
    store.close()
    scanner2, fetcher2, store2 = build(config, db, OK_PAGES, clock)
    third = await scanner2.search_marketplace("example", "bike", limit=2)
    assert third["status"] == "cached" and third["listings"] == first["listings"]
    assert fetcher2.requests == [] and store2.status()["responses"] == 1


async def test_limit_is_applied_when_serving_so_a_larger_limit_needs_no_request(config, tmp_path):
    scanner, fetcher, _ = build(config, tmp_path / "ps.db", OK_PAGES, Clock())
    small = await scanner.search_marketplace("example", "bike", limit=1)
    assert small["status"] == "ok" and small["count"] == 1
    large = await scanner.search_marketplace("example", "bike", limit=10)
    assert large["status"] == "cached" and large["count"] == 3 and len(fetcher.requests) == 1


async def test_block_cooldown_survives_restart_and_stored_response_is_served(config, tmp_path):
    clock, db = Clock(), tmp_path / "ps.db"
    scanner, fetcher, store = build(config, db, OK_PAGES, clock, search_ttl=600)
    good = await scanner.search_marketplace("example", "bike")
    assert good["status"] == "ok" and len(fetcher.requests) == 1

    clock.advance(601)  # the stored response is stale, so a live request is allowed...
    fetcher.pages = dict(BLOCK_PAGES)  # ...and the site now answers with a challenge page
    out = await scanner.search_marketplace("example", "bike")
    assert len(fetcher.requests) == 2
    assert out["status"] == "cooldown" and out["from_cache"] is True
    assert out["listings"] == good["listings"] and out["cached_at"] == good["fetched_at"]
    assert out["cooldown_reason"] == "challenge_page"
    assert 0 < out["cooldown_remaining_seconds"] <= 3601
    assert out["retry_after_seconds"] == out["cooldown_remaining_seconds"]
    assert out["cooldown_until"] == iso(clock.now + timedelta(seconds=3600))
    assert out["next_refresh_at"] == out["cooldown_until"]

    # Another target on the same source, nothing stored: blocked, no listings, no request.
    other = await scanner.search_marketplace("example", "other")
    assert other["status"] == "blocked" and other["listings"] == [] and other["count"] == 0
    assert other["retry_after_seconds"] > 0 and other["cooldown_reason"] == "challenge_page"
    assert other["from_cache"] is False and other["cached_at"] is None
    assert len(fetcher.requests) == 2

    # Restart: close and reopen the database with a fresh fetcher and scanner.
    store.close()
    scanner2, fetcher2, store2 = build(config, db, OK_PAGES, clock, search_ttl=600)
    assert store2.status()["active_cooldowns"] == 1
    again = await scanner2.search_marketplace("example", "bike")
    assert again["status"] == "cooldown" and again["listings"] == good["listings"]
    assert again["cooldown_remaining_seconds"] > 0 and again["cooldown_reason"] == "challenge_page"
    assert fetcher2.requests == []
    health = await scanner2.health(probe=True)
    entry = next(s for s in health["sources"] if s["key"] == "example")
    assert entry["state"] == "blocked" and entry["reason"] == "challenge_page"
    assert fetcher2.requests == []  # a cooling-down source is not probed either
    assert health["store"]["active_cooldowns"] == 1 and health["store"]["responses"] == 1


async def test_fetch_listing_in_cooldown_serves_the_stored_listing(config, tmp_path):
    clock = Clock()
    scanner, fetcher, _ = build(config, tmp_path / "ps.db", OK_PAGES, clock, fetch_ttl=100)
    ok = await scanner.fetch_listing(LISTING_URL)
    assert ok["status"] == "ok" and ok["target"] == LISTING_URL and ok["from_cache"] is False
    clock.advance(101)
    fetcher.pages = dict(BLOCK_PAGES)
    out = await scanner.fetch_listing(LISTING_URL)
    assert out["status"] == "cooldown" and out["listing"] == ok["listing"]
    assert out["cooldown_reason"] == "http_403" and out["cooldown_remaining_seconds"] > 0
    other = await scanner.fetch_listing("https://shop.example/item/other-1004.html")
    assert other["status"] == "blocked" and "listing" not in other and other["retry_after_seconds"] > 0
    assert len(fetcher.requests) == 2


async def test_expired_cooldown_is_pruned_and_a_live_fetch_is_allowed_again(config, tmp_path):
    clock, db = Clock(), tmp_path / "ps.db"
    store = Store(db, clock=clock)
    assert store.put_cooldown("example", "http_403", clock.now - timedelta(seconds=5), 3600)
    store.close()
    scanner, fetcher, store2 = build(config, db, OK_PAGES, clock)
    assert store2.active_cooldowns() == [] and store2.status()["active_cooldowns"] == 0
    with sqlite3.connect(db) as raw:
        assert raw.execute("SELECT COUNT(*) FROM cooldowns").fetchone()[0] == 0  # pruned at startup
    out = await scanner.search_marketplace("example", "bike")
    assert out["status"] == "ok" and len(fetcher.requests) == 1
    assert out["cooldown_until"] is None and out["cooldown_remaining_seconds"] == 0


async def test_freshness_windows_are_separate_for_search_and_fetch(config, tmp_path):
    clock = Clock()
    scanner, fetcher, _ = build(config, tmp_path / "ps.db", OK_PAGES, clock, search_ttl=100, fetch_ttl=1000)
    assert (await scanner.search_marketplace("example", "bike"))["status"] == "ok"
    assert (await scanner.fetch_listing(LISTING_URL))["status"] == "ok"
    assert len(fetcher.requests) == 2
    clock.advance(500)
    search = await scanner.search_marketplace("example", "bike")
    fetch = await scanner.fetch_listing(LISTING_URL)
    assert search["status"] == "ok" and search["from_cache"] is False  # 100 s window passed: one live request
    assert fetch["status"] == "cached" and fetch["from_cache"] is True and fetch["age_seconds"] == 500
    assert fetch["listing"]["posted_date"] is None and fetch["next_refresh_at"] == plus(fetch["cached_at"], 1000)
    assert len(fetcher.requests) == 3
    clock.advance(600)
    assert (await scanner.fetch_listing(LISTING_URL))["status"] == "ok" and len(fetcher.requests) == 4


async def test_query_normalisation_makes_trivially_different_queries_one_target(config, tmp_path):
    assert normalize_query("  Red   BIKE ") == "red bike"
    assert normalize_query("Straße\tund\nWEG") == "strasse und weg"
    assert search_target("Red Bike", "newest") == "newest:red bike"
    assert search_target("red bike", "price_asc") != search_target("red bike", "newest")
    scanner, fetcher, _ = build(config, tmp_path / "ps.db", OK_PAGES, Clock())
    a = await scanner.search_marketplace("example", "Red Bike")
    b = await scanner.search_marketplace("example", "  red   bike ")
    assert a["status"] == "ok" and b["status"] == "cached"
    assert a["target"] == b["target"] == "newest:red bike"
    assert b["query"] == "  red   bike "  # the caller's own text is echoed back
    assert len(fetcher.requests) == 1 and fetcher.requests[0] == "https://market.example/search?q=red%20bike&order=new"
    c = await scanner.search_marketplace("example", "red bike", sort="price_asc")  # another results page
    assert c["status"] == "ok" and c["target"] == "price_asc:red bike" and len(fetcher.requests) == 2


async def test_live_requests_are_minimised(config, tmp_path):
    """Counts live requests: cached = 0; concurrent identical = at most 1; blocked = 0 and no retry;
    a search never fetches the listings it found."""
    scanner, fetcher, _ = build(config, tmp_path / "ps.db", OK_PAGES, Clock(), slow=True)

    results = await asyncio.gather(*(scanner.search_marketplace("example", "bike", limit=2) for _ in range(5)))
    assert len(fetcher.requests) == 1  # five concurrent identical callers, one live request
    assert {r["status"] for r in results} == {"ok"}
    assert all(r["listings"] == results[0]["listings"] and r["count"] == 2 for r in results)
    assert fetcher.requests == ["https://market.example/search?q=bike&order=new"]  # no listing fetched afterwards

    cached = await scanner.search_marketplace("example", "bike")
    assert cached["status"] == "cached" and len(fetcher.requests) == 1  # cached call: zero live requests

    fetcher.pages = dict(BLOCK_PAGES)
    blocked = await scanner.search_marketplace("example", "something new")
    assert blocked["status"] == "blocked" and len(fetcher.requests) == 2  # the one request that was refused
    for _ in range(5):
        again = await scanner.search_marketplace("example", "something new")
        assert again["status"] == "blocked" and again["listings"] == []
    await asyncio.gather(*(scanner.fetch_listing(LISTING_URL) for _ in range(3)))
    (await scanner.health(probe=True))
    assert len(fetcher.requests) == 2  # blocked: zero live requests, no retry, no probe


async def test_store_unavailable_fails_closed(config):
    fetcher = CountingFetcher(config, OK_PAGES)
    scanner = PriceScanner(config, fetcher, store=None, store_path=Path("/nowhere/x.db"),
                           store_error="cannot open the persistent store at /nowhere/x.db: unable to open database file")
    out = await scanner.search_marketplace("example", "bike")
    assert out["status"] == "error" and "persistent store unavailable" in out["reason"] and out["listings"] == []
    fetched = await scanner.fetch_listing(LISTING_URL)
    assert fetched["status"] == "error" and "persistent store unavailable" in fetched["reason"]
    assert fetcher.requests == []
    assert scanner.list_sources()["count"] == 3
    health = await scanner.health(probe=False)
    assert health["status"] == "degraded" and health["store"]["reachable"] is False
    assert health["store"]["path"] == "/nowhere/x.db" and health["store"]["responses"] is None
    assert fetcher.requests == []


def test_store_refuses_a_path_it_cannot_open(tmp_path):
    with pytest.raises(StoreError):
        Store(tmp_path)  # a directory, not a file
    (tmp_path / "junk.db").write_text("not a database at all", encoding="utf-8")
    with pytest.raises(StoreError):
        Store(tmp_path / "junk.db")


async def test_write_failures_are_reported_and_repeated_failures_refuse_live_requests(config, tmp_path):
    scanner, fetcher, store = build(config, tmp_path / "ps.db", OK_PAGES, Clock())

    def failing_write(what, statements):
        store._record_write_failure(what, sqlite3.OperationalError("disk I/O error"))
        return False

    store._write = failing_write  # the disk goes bad
    first = await scanner.search_marketplace("example", "q1")
    assert first["status"] == "ok" and "store_warning" in first and store.write_failures == 1
    assert (await scanner.search_marketplace("example", "q1"))["status"] == "ok"  # not stored: fetched again, once
    assert (await scanner.search_marketplace("example", "q2"))["status"] == "ok"
    assert store.consecutive_write_failures == 3 and store.degraded and len(fetcher.requests) == 3
    refused = await scanner.search_marketplace("example", "q3")
    assert refused["status"] == "error" and "failing writes" in refused["reason"] and len(fetcher.requests) == 3
    health = await scanner.health(probe=False)
    assert health["status"] == "degraded" and health["store"]["degraded"] is True
    assert health["store"]["write_failures"] == 4 and "disk I/O error" in health["store"]["last_error"]

    del store._write  # the disk is back
    recovered = await scanner.search_marketplace("example", "q3")
    assert recovered["status"] == "ok" and "store_warning" not in recovered and len(fetcher.requests) == 4
    assert store.consecutive_write_failures == 0 and not store.degraded
    assert (await scanner.search_marketplace("example", "q3"))["status"] == "cached"
    assert (await scanner.health(probe=False))["status"] == "ok"


def test_database_file_is_private_and_in_wal_mode(tmp_path):
    db = tmp_path / "nested" / "dir" / "ps.db"
    store = Store(db)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    status = store.status()
    assert status["reachable"] is True and status["journal_mode"] == "wal"
    assert status["responses"] == 0 and status["active_cooldowns"] == 0 and status["write_failures"] == 0
    with sqlite3.connect(db) as raw:
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"responses", "cooldowns"} <= tables
    store.close()


async def test_health_reports_the_store_without_its_contents(config, tmp_path):
    scanner, _, _ = build(config, tmp_path / "ps.db", OK_PAGES, Clock(), search_ttl=3600, fetch_ttl=7200)
    await scanner.search_marketplace("example", "bike")
    health = await scanner.health(probe=False)
    assert health["status"] == "ok" and health["freshness_seconds"] == {"search": 3600, "fetch": 7200}
    store = health["store"]
    assert store["path"] == str(tmp_path / "ps.db") and store["reachable"] is True
    assert store["responses"] == 1 and store["active_cooldowns"] == 0 and store["degraded"] is False
    assert "response" not in store and "listings" not in str(store)
