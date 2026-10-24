"""The four tools, independent of the MCP transport (so they are easy to test)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from price_scanner import __version__
from price_scanner.extract import parse_listing_page, parse_search_page
from price_scanner.fetcher import Fetcher, FetchResult
from price_scanner.parsers import get_parser
from price_scanner.schema import utc_now_iso
from price_scanner.sources import SORT_KEYS, Source, SourcesConfig
from price_scanner.urlsafety import UnsafeURL, sanitize_listing_url

log = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 200
PROBE_TTL_SECONDS = 600


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
                 profile_dir: Path | None = None) -> None:
        self.config = config
        self.fetcher = fetcher
        self.config_name = config_name
        self.profile_dir = profile_dir
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
        query = (query or "").strip()
        if not query or len(query) > MAX_QUERY_LENGTH:
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
        url, sort_applied = build_search_url(source, query, sort)
        result = await self.fetcher.fetch(source, url, wait_for=source.search.wait_for)
        if result.verdict.state != "ok":
            return {**base, **self._blocked(result), "search_url": url}

        flag_rules = self._flag_rules(source)
        if parser is not None and hasattr(parser, "parse_search"):
            listings = parser.parse_search(result.html, source, tz=timing.timezone, flag_rules=flag_rules, limit=limit)
        else:
            listings = parse_search_page(result.html, source, tz=timing.timezone, flag_rules=flag_rules, limit=limit)
        out = {
            **base,
            "status": "ok",
            "search_url": url,
            "sort_applied": sort_applied,
            "dates_reliable": source.search.dates_reliable,
            "from_cache": result.from_cache,
            "listings": [listing.model_dump() for listing in listings],
            "count": len(listings),
            "fetched_at": utc_now_iso(),
        }
        if not listings:
            out["note"] = "no listings parsed: nothing matched, or the page layout changed"
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
        result = await self.fetcher.fetch(source, url, wait_for=source.listing.wait_for)
        if result.verdict.state != "ok":
            return {**base, **self._blocked(result)}
        parser = get_parser(source.parser) if source.parser else None
        flag_rules = self._flag_rules(source)
        if parser is not None and hasattr(parser, "parse_listing"):
            listing = parser.parse_listing(result.html, url, source, tz=timing.timezone, flag_rules=flag_rules)
        else:
            listing = parse_listing_page(result.html, url, source, tz=timing.timezone, flag_rules=flag_rules)
        if listing is None:
            return {**base, "status": "error", "reason": "no listing found on the page"}
        return {**base, "status": "ok", "from_cache": result.from_cache, "listing": listing.model_dump()}

    async def _probe(self, source: Source) -> None:
        st = self.fetcher.state(source.key)
        if st.last_checked:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(st.last_checked)).total_seconds()
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
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": int(time.monotonic() - self.started),
            "config": self.config_name,
            "browser": "running" if self.fetcher.browser_running else "not started",
            "probed": probe,
            "sources": entries,
        }
