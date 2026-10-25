"""The four tools, independent of the MCP transport (so they are easy to test).

The persistent store (:mod:`price_scanner.store`) sits in front of every live
request. The rule it serves, before any convenience: as few live requests as
possible, and a source's "no" is respected.

- The store is checked first, on every search and fetch. A live request is the
  last resort, never the default.
- A *target* (one source plus one normalised query, or one source plus one
  listing URL) is fetched live at most once per freshness window. Inside the
  window the stored response is served, flagged ``cached``.
- While a source is in its block cooldown nothing is sent to it. The latest
  stored response is served flagged ``cooldown``; with none stored the reply is
  ``blocked``. No retry, no rotation, no evasion, no background refresh.
- Concurrent identical targets share one live request.
- A search never fetches the listings it found; ``fetch_listing`` fetches only
  the one URL the caller asked for.
- If the store cannot be opened, or keeps failing to write, search and fetch
  are refused (fail closed) instead of falling back to unlimited live fetching.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, quote_plus

from price_scanner import __version__
from price_scanner.extract import parse_listing_page, parse_search_page
from price_scanner.fetcher import Fetcher, FetchResult
from price_scanner.parsers import get_parser
from price_scanner.sources import SORT_KEYS, Source, SourcesConfig
from price_scanner.store import Store, StoreError, StoredResponse, iso, parse_iso
from price_scanner.urlsafety import UnsafeURL, sanitize_listing_url

log = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 200
PROBE_TTL_SECONDS = 600
DEFAULT_SEARCH_TTL_SECONDS = 6 * 3600
DEFAULT_FETCH_TTL_SECONDS = 24 * 3600

# The fields every response that involves a target carries, in their
# "live result or error" form. Served-from-store responses override them.
NO_CACHE: dict[str, Any] = {"from_cache": False, "cached_at": None, "age_seconds": 0, "next_refresh_at": None}
NO_COOLDOWN: dict[str, Any] = {"cooldown_until": None, "cooldown_remaining_seconds": 0, "cooldown_reason": None}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def normalize_query(query: str) -> str:
    """The normalised search query: trimmed, every run of whitespace collapsed
    to one space, case-folded. ``"  Red   BIKE "`` and ``"red bike"`` are one
    target. Nothing else changes: no accent stripping, no stemming."""
    return " ".join(query.split()).casefold()


def search_target(query: str, sort: str) -> str:
    """``<sort>:<normalised query>``. A different sort order is a different
    results page, so it is a different target."""
    return f"{sort}:{normalize_query(query)}"


def _slug(query: str) -> str:
    return "-".join(quote(part.lower(), safe="") for part in query.split())


def build_search_url(source: Source, query: str, sort: str) -> tuple[str, bool]:
    assert source.search is not None
    url = (
        source.search.url.replace("{query_plus}", quote_plus(query))
        .replace("{query_slug}", _slug(query))
        .replace("{query}", quote(query, safe=""))
    )
    fragment = source.search.sort.get(sort)
    if fragment:
        url += ("&" if "?" in url else "?") + fragment
    return url, bool(fragment)


class PriceScanner:
    def __init__(self, config: SourcesConfig, fetcher: Fetcher, *, config_name: str = "sources.yaml",
                 profile_dir: Path | None = None, store: Store | None = None, store_error: str | None = None,
                 store_path: Path | str | None = None,
                 search_ttl_seconds: int = DEFAULT_SEARCH_TTL_SECONDS,
                 fetch_ttl_seconds: int = DEFAULT_FETCH_TTL_SECONDS,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.config = config
        self.fetcher = fetcher
        self.config_name = config_name
        self.profile_dir = profile_dir
        self.store = store
        self.store_error = None if store is not None else (store_error or "no persistent store configured")
        self.store_path = str(store.path) if store is not None else (str(store_path) if store_path else None)
        self.ttl = {"search": max(0, int(search_ttl_seconds)), "fetch": max(0, int(fetch_ttl_seconds))}
        self._now = clock or _utc_now
        self._inflight: dict[tuple[str, str, str], asyncio.Task[dict[str, Any]]] = {}
        self.started = time.monotonic()

    # -- helpers ------------------------------------------------------------

    def _flag_rules(self, source: Source):
        return self.config.defaults.flags + source.flags

    def _unavailable(self, source: Source) -> dict[str, Any] | None:
        if not source.enabled:
            return {"status": "disabled", "reason": "source is disabled in the config"}
        if source.login_required and self.profile_dir is None:
            return {"status": "login-required", "reason": "no browser profile configured"}
        return None

    @staticmethod
    def _blocked(result: FetchResult) -> dict[str, Any]:
        out: dict[str, Any] = {"status": result.verdict.state, "reason": result.verdict.reason}
        if result.retry_after_seconds:
            out["retry_after_seconds"] = result.retry_after_seconds
        return out

    # -- store first, cooldown second, one live request last -----------------

    def _cooldown_fields(self, source: Source) -> dict[str, Any]:
        st = self.fetcher.state(source.key)
        remaining = st.cooling_down()
        if remaining:
            return {"cooldown_until": st.blocked_until_wall, "cooldown_remaining_seconds": remaining,
                    "cooldown_reason": st.block_reason}
        return dict(NO_COOLDOWN)

    def _from_store(self, status: str, source: Source, kind: str, stored: StoredResponse) -> dict[str, Any]:
        """The stored response served as ``cached`` or ``cooldown``. No request is made."""
        now = self._now()
        cooldown = self._cooldown_fields(source)
        refresh = stored.fetched_at + timedelta(seconds=self.ttl[kind])
        out = dict(stored.response)
        out.pop("store_warning", None)
        if cooldown["cooldown_until"]:
            refresh = max(refresh, parse_iso(cooldown["cooldown_until"]))
            out["retry_after_seconds"] = cooldown["cooldown_remaining_seconds"]
        out.update(status=status, from_cache=True, cached_at=iso(stored.fetched_at),
                   age_seconds=stored.age_seconds(now), next_refresh_at=iso(refresh), **cooldown)
        return out

    def _blocked_without_store(self, source: Source, base: dict[str, Any],
                               result: FetchResult | None = None) -> dict[str, Any]:
        """Cooling down and nothing stored: ``blocked``, no listings, no request."""
        cooldown = self._cooldown_fields(source)
        st = self.fetcher.state(source.key)
        reason = (result.verdict.reason if result is not None else None) or st.block_reason or "cooling_down"
        retry = (result.retry_after_seconds if result is not None else None) or cooldown["cooldown_remaining_seconds"]
        return {**base, "status": "blocked", "reason": reason, "retry_after_seconds": retry,
                **NO_CACHE, "next_refresh_at": cooldown["cooldown_until"], **cooldown}

    def _answer_without_network(self, kind: str, source: Source, stored: StoredResponse | None,
                                base: dict[str, Any]) -> dict[str, Any] | None:
        """What the store alone answers: a cooldown, or a response still inside its window."""
        if self.fetcher.state(source.key).cooling_down():
            if stored is not None:
                return self._from_store("cooldown", source, kind, stored)
            return self._blocked_without_store(source, base)
        if stored is not None and stored.age_seconds(self._now()) < self.ttl[kind]:
            return self._from_store("cached", source, kind, stored)
        return None

    async def _coalesced(self, key: tuple[str, str, str],
                         produce: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """Concurrent callers of one target share one live request."""
        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(produce())
            self._inflight[key] = task

            def _forget(done: asyncio.Task, key: tuple[str, str, str] = key) -> None:
                if self._inflight.get(key) is done:
                    self._inflight.pop(key, None)

            task.add_done_callback(_forget)
        return await asyncio.shield(task)

    async def _resolve(self, kind: str, source: Source, target: str, base: dict[str, Any],
                       live: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        if self.store is None:
            return {**base, "status": "error",
                    "reason": f"persistent store unavailable ({self.store_error}); search and fetch are refused "
                              "until it can be opened. list_sources and health still work.",
                    **NO_CACHE, **NO_COOLDOWN}
        try:
            stored = self.store.get_response(source.key, kind, target)
        except StoreError as exc:
            return {**base, "status": "error",
                    "reason": f"{exc}; no live request is made while the store is unreadable",
                    **NO_CACHE, **NO_COOLDOWN}
        answer = self._answer_without_network(kind, source, stored, base)
        if answer is not None:
            return answer
        if self.store.degraded and not self.store.check_writable():
            return {**base, "status": "error",
                    "reason": f"persistent store is failing writes ({self.store.consecutive_write_failures} in a "
                              "row); live requests are refused until a write succeeds",
                    **NO_CACHE, **NO_COOLDOWN}
        return dict(await self._coalesced((source.key, kind, target), live))

    async def _live(self, kind: str, source: Source, target: str, url: str, wait_for: str | None,
                    build: Callable[[FetchResult], dict[str, Any] | None], base: dict[str, Any]) -> dict[str, Any]:
        """One live request, shared by every concurrent caller of the same target.

        A block starts the (persisted) cooldown inside the fetcher; the caller
        then gets the latest stored response or ``blocked``. Nothing is retried.
        """
        result = await self.fetcher.fetch(source, url, wait_for=wait_for)
        if result.verdict.state == "blocked":
            try:
                stored = self.store.get_response(source.key, kind, target) if self.store else None
            except StoreError:
                stored = None
            if stored is not None and self.fetcher.state(source.key).cooling_down():
                return self._from_store("cooldown", source, kind, stored)
            return self._blocked_without_store(source, base, result)
        if result.verdict.state != "ok":
            return {**base, **self._blocked(result), **NO_CACHE, **NO_COOLDOWN}
        payload = build(result)
        if payload is None:
            return {**base, "status": "error", "reason": "no listing found on the page", **NO_CACHE, **NO_COOLDOWN}
        fetched_at = parse_iso(result.fetched_at) if result.fetched_at else self._now()
        out = {**base, **payload, "status": "ok", "fetched_at": iso(fetched_at), **NO_CACHE,
               "next_refresh_at": iso(fetched_at + timedelta(seconds=self.ttl[kind])), **NO_COOLDOWN}
        if result.from_cache:  # the fetcher's short in-memory cache answered: not a live request either
            out.update(status="cached", from_cache=True, cached_at=iso(fetched_at),
                       age_seconds=max(0, int((self._now() - fetched_at).total_seconds())))
        assert self.store is not None
        if not self.store.put_response(source.key, kind, target, out, fetched_at,
                                       self.fetcher.state(source.key).last_state):
            out["store_warning"] = f"response not stored ({self.store.last_error}); it will be fetched again next time"
        return out

    # -- tools --------------------------------------------------------------

    def list_sources(self) -> dict[str, Any]:
        sources = []
        for source in self.config.sources.values():
            parser = get_parser(source.parser) if source.parser else None
            can_search = source.search is not None or hasattr(parser, "parse_search")
            sources.append(
                {
                    "key": source.key,
                    "name": source.name,
                    "enabled": source.enabled,
                    "search": can_search,
                    "sort_options": sorted(source.search.sort) if source.search else [],
                    "dates_reliable": bool(source.search and source.search.dates_reliable),
                    "listing_dates_reliable": source.listing.dates_reliable,
                    "login_required": source.login_required,
                    "fetch_mode": source.fetch_mode,
                    "domains": source.domains,
                    "notes": source.notes,
                }
            )
        return {"sources": sources, "count": len(sources)}

    async def search_marketplace(self, site: str, query: str, limit: int = 20, sort: str = "newest") -> dict[str, Any]:
        base: dict[str, Any] = {"site": site, "query": query, "sort": sort, "listings": [], "count": 0}
        source = self.config.sources.get(site)
        if source is None:
            return {**base, "status": "error", "reason": "unknown source",
                    "known_sources": sorted(self.config.sources)}
        text = (query or "").strip()
        if not text or len(text) > MAX_QUERY_LENGTH:
            return {**base, "status": "error", "reason": f"query must be 1-{MAX_QUERY_LENGTH} characters"}
        if sort not in SORT_KEYS:
            return {**base, "status": "error", "reason": f"sort must be one of {list(SORT_KEYS)}"}
        unavailable = self._unavailable(source)
        if unavailable:
            return {**base, **unavailable}
        parser = get_parser(source.parser) if source.parser else None
        if source.search is None:
            return {**base, "status": "error", "reason": "search is not supported for this source"}

        timing = self.config.timing(source)
        limit = max(1, min(int(limit), timing.max_results))
        target = search_target(text, sort)
        url, sort_applied = build_search_url(source, normalize_query(text), sort)
        base_zero = {**base, "target": target, "search_url": url}

        def build(result: FetchResult) -> dict[str, Any]:
            flag_rules = self._flag_rules(source)
            # The whole page (up to the source's max_results) is parsed and stored,
            # so a later call with a larger limit is served from the store.
            if parser is not None and hasattr(parser, "parse_search"):
                listings = parser.parse_search(result.html, source, tz=timing.timezone, flag_rules=flag_rules,
                                               limit=timing.max_results)
            else:
                listings = parse_search_page(result.html, source, tz=timing.timezone, flag_rules=flag_rules,
                                             limit=timing.max_results)
            payload: dict[str, Any] = {
                "search_url": url,
                "sort_applied": sort_applied,
                "dates_reliable": source.search.dates_reliable,
                "listings": [listing.model_dump() for listing in listings],
                "count": len(listings),
            }
            if not listings:
                payload["note"] = "no listings parsed: nothing matched, or the page layout changed"
            return payload

        out = await self._resolve("search", source, target, base_zero,
                                  lambda: self._live("search", source, target, url, source.search.wait_for,
                                                     build, base_zero))
        out.update(site=site, query=query, sort=sort, target=target)  # this caller's own words
        listings = out.get("listings") or []
        out["listings"] = listings[:limit]
        out["count"] = len(out["listings"])
        return out

    async def fetch_listing(self, url: str) -> dict[str, Any]:
        base: dict[str, Any] = {"url": url}
        try:
            url = sanitize_listing_url(url)  # rebuilt; only this string is fetched
        except UnsafeURL as exc:
            return {**base, "status": "error", "reason": str(exc)}
        base["url"] = url
        source = self.config.source_for_url(url)
        if source is None:
            return {**base, "status": "error", "reason": "no configured source matches this URL"}
        base["site"] = source.key
        unavailable = self._unavailable(source)
        if unavailable:
            return {**base, **unavailable}
        timing = self.config.timing(source)
        target = url  # the sanitised URL is the normalised target
        base_zero = {**base, "target": target}

        def build(result: FetchResult) -> dict[str, Any] | None:
            parser = get_parser(source.parser) if source.parser else None
            flag_rules = self._flag_rules(source)
            if parser is not None and hasattr(parser, "parse_listing"):
                listing = parser.parse_listing(result.html, url, source, tz=timing.timezone, flag_rules=flag_rules)
            else:
                listing = parse_listing_page(result.html, url, source, tz=timing.timezone, flag_rules=flag_rules)
            if listing is None:
                return None
            return {"listing": listing.model_dump()}

        out = await self._resolve("fetch", source, target, base_zero,
                                  lambda: self._live("fetch", source, target, url, source.listing.wait_for,
                                                     build, base_zero))
        out.update(url=url, site=source.key, target=target)
        return out

    async def _probe(self, source: Source) -> None:
        st = self.fetcher.state(source.key)
        if st.last_checked:
            age = (self._now() - parse_iso(st.last_checked)).total_seconds()
            if age < PROBE_TTL_SECONDS and st.last_state != "unknown":
                return
        await self.fetcher.fetch(source, source.health_url or source.base_url)

    async def health(self, probe: bool = True) -> dict[str, Any]:
        to_probe = []
        for source in self.config.sources.values():
            if probe and self._unavailable(source) is None and not self.fetcher.state(source.key).cooling_down():
                to_probe.append(source)
        if to_probe:
            await asyncio.gather(*(self._probe(s) for s in to_probe))

        entries = []
        for source in self.config.sources.values():
            st = self.fetcher.state(source.key)
            entry: dict[str, Any] = {"key": source.key, "enabled": source.enabled}
            unavailable = self._unavailable(source)
            remaining = st.cooling_down()
            if unavailable:
                entry["state"] = unavailable["status"]
                entry["reason"] = unavailable["reason"]
            elif remaining:
                entry.update(state="blocked", reason=st.block_reason, retry_after_seconds=remaining,
                             blocked_until=st.blocked_until_wall)
            else:
                entry.update(state=st.last_state, reason=st.last_reason)
            entry["last_checked"] = st.last_checked
            entries.append(entry)

        if self.store is not None:
            store = self.store.status()
            healthy = bool(store["reachable"]) and not self.store.degraded
        else:
            store = {"path": self.store_path, "reachable": False, "error": self.store_error,
                     "responses": None, "active_cooldowns": None, "degraded": True}
            healthy = False
        return {
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            "uptime_seconds": int(time.monotonic() - self.started),
            "config": self.config_name,
            "browser": "running" if self.fetcher.browser_running else "not started",
            "probed": probe,
            "freshness_seconds": dict(self.ttl),
            "store": store,
            "sources": entries,
        }
