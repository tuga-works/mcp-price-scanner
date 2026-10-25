"""FastMCP server: four read-only tools over streamable HTTP, behind a bearer token."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from pydantic import Field

from price_scanner import __version__
from price_scanner.auth import BearerAuthMiddleware
from price_scanner.fetcher import Fetcher
from price_scanner.logging_setup import setup_logging
from price_scanner.service import PriceScanner
from price_scanner.settings import Settings, SettingsError
from price_scanner.sources import load_sources
from price_scanner.store import Store, StoreError

log = logging.getLogger("price_scanner")

INSTRUCTIONS = """\
Read-only marketplace reader. Call health() once before searching. Use
list_sources() to see which sources are enabled and whether their dates are
reliable. search_marketplace() returns listings as shown on the page; a
listing with posted_date null has no date on the page. Results are served
from a persistent store whenever possible, to keep requests to the sites to
a minimum: status "cached" is a stored result still inside its freshness
window (next_refresh_at says when a live check becomes possible), status
"cooldown" is the latest stored result while the site is refusing requests
(see cooldown_until and cooldown_remaining_seconds), and status "blocked"
means the site refused and nothing is stored. Never retry a cooldown or
blocked result; wait. The bridge never buys, bids, or contacts anyone."""

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


def build_server(scanner: PriceScanner, store: Store | None = None) -> Any:
    from fastmcp import FastMCP

    @asynccontextmanager
    async def lifespan(_server: Any):
        try:
            yield {}
        finally:
            await scanner.fetcher.close()
            if store is not None:
                store.close()

    mcp = FastMCP("price-scanner", instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan)

    @mcp.tool(annotations=READ_ONLY)
    async def search_marketplace(
        site: Annotated[str, Field(description="Source key from list_sources(), e.g. 'olx'")],
        query: Annotated[str, Field(description="Search text, 1-200 characters")],
        limit: Annotated[int, Field(ge=1, le=100, description="Maximum listings to return")] = 20,
        sort: Annotated[
            Literal["newest", "relevance", "price_asc", "price_desc"],
            Field(description="Ordering, applied where the source supports it"),
        ] = "newest",
    ) -> dict[str, Any]:
        """Search one configured source and return structured listings (first results page only).

        Served from the persistent store when the same search was made inside
        its freshness window (status "cached") or while the source is cooling
        down after a block (status "cooldown"); a live request is made only
        otherwise.
        """
        return await scanner.search_marketplace(site, query, limit, sort)

    @mcp.tool(annotations=READ_ONLY)
    async def fetch_listing(
        url: Annotated[str, Field(description="Absolute URL of one listing on a configured source")],
    ) -> dict[str, Any]:
        """Fetch one listing page and return its structured fields (served from the store inside its freshness window)."""
        return await scanner.fetch_listing(url)

    @mcp.tool(annotations={**READ_ONLY, "openWorldHint": False})
    async def list_sources() -> dict[str, Any]:
        """List configured sources and their capabilities. Sends no requests."""
        return scanner.list_sources()

    @mcp.tool(annotations=READ_ONLY)
    async def health(
        probe: Annotated[
            bool,
            Field(description="Check reachability with one light request to each enabled source's home page "
                              "(results reused for 10 minutes). False reports the last known state only."),
        ] = True,
    ) -> dict[str, Any]:
        """Server status, persistent-store state (path, reachable, counts) and per-source reachability
        (reachable / blocked / login-required). Never fetches listings."""
        return await scanner.health(probe)

    return mcp


def create_app(settings: Settings) -> Any:
    config, used_path = load_sources(settings.config_path)

    store: Store | None = None
    store_error: str | None = None
    try:
        store = Store(settings.db_path)
    except StoreError as exc:
        store_error = str(exc)
        log.error("%s. Failing closed: search and fetch are refused until the store can be opened; "
                  "list_sources and health still work.", exc)

    fetcher = Fetcher(config, headless=settings.headless, profile_dir=settings.profile_dir,
                      cache_ttl_seconds=settings.cache_ttl_seconds, store=store)
    if store is not None:
        try:
            restored = fetcher.restore_cooldowns()
            log.info("persistent store: %d active cooldown(s) restored", restored)
        except StoreError as exc:
            store_error = str(exc)
            log.error("%s. Failing closed: search and fetch are refused.", exc)
            store.close()
            store = None
            fetcher.store = None

    scanner = PriceScanner(config, fetcher, config_name=used_path.name, profile_dir=settings.profile_dir,
                           store=store, store_error=store_error, store_path=settings.db_path,
                           search_ttl_seconds=settings.search_cache_ttl_seconds,
                           fetch_ttl_seconds=settings.fetch_cache_ttl_seconds)
    mcp = build_server(scanner, store)

    from starlette.middleware import Middleware

    app = mcp.http_app(
        path="/mcp",
        middleware=[Middleware(BearerAuthMiddleware, token=settings.token)],
        allowed_hosts=list(settings.allowed_hosts) or None,
    )
    enabled = [s.key for s in config.sources.values() if s.enabled]
    log.info("config %s: %d sources, enabled: %s", used_path.name, len(config.sources), ", ".join(enabled) or "none")
    log.info("freshness windows: search %ds, fetch %ds; in-memory page cache %ds",
             settings.search_cache_ttl_seconds, settings.fetch_cache_ttl_seconds, settings.cache_ttl_seconds)
    return app


def main() -> None:
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        setup_logging("INFO")
        log.error("%s", exc)
        sys.exit(2)
    redactor = setup_logging(settings.log_level, secrets=[settings.token])
    if settings.profile_dir is not None:
        redactor.add_secret(str(settings.profile_dir))
    app = create_app(settings)

    import uvicorn

    log.info("price-scanner %s listening on %s:%d (path /mcp)", __version__, settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, proxy_headers=False,
                server_header=False)


if __name__ == "__main__":
    main()
