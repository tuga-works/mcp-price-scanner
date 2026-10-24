"""Polite page fetching: a real browser (Playwright) or plain HTTP (curl_cffi).

Politeness rules, enforced here for every request:

- one request at a time per site (a per-site lock, shared by all tool calls);
- a random delay of ``min_delay``..``max_delay`` seconds (never zero) since the
  previous request to the same site;
- a 403, 429, challenge page or CAPTCHA wall stops the source immediately and
  starts a cooldown (default one hour) during which no request is sent;
- the browser is a normal, current Chromium with its default user agent; no
  user-agent rotation, no fingerprint changes, no CAPTCHA solving;
- fetched pages are cached in memory for a few minutes only.

SSRF protection: every request (navigations, redirect hops, subresources)
leaves through :class:`~price_scanner.egress.EgressGuard`, which only tunnels
TLS to public addresses. Document navigations outside the source's domains
are aborted, and a page whose final URL left the source's domains is
discarded.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from price_scanner.blocking import Verdict, classify
from price_scanner.egress import EgressGuard
from price_scanner.schema import utc_now_iso
from price_scanner.sources import Source, SourcesConfig
from price_scanner.urlsafety import address_allowed, host_matches, is_ip_literal, normalize_host

log = logging.getLogger(__name__)

WAIT_FOR_TIMEOUT_MS = 10_000
CACHE_MAX_ENTRIES = 256
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_NET_ERROR_RE = re.compile(r"net::(ERR_[A-Z_]+)")


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return normalize_host(urlsplit(url).hostname)
    except ValueError:
        return ""


def make_route_handler(domains: list[str], block_resources: bool):
    """Playwright route handler: strict for documents, cheap checks for the rest.

    - document requests (top-level and frames) must stay on the source's domains;
    - nothing may target an IP literal that is not a public address;
    - images, media and fonts are skipped when ``block_resources`` is set.
    Redirect hops never reach route handlers; the egress guard covers them.
    """

    async def _route(route: Any) -> None:
        request = route.request
        host = _host(request.url)
        if request.resource_type == "document" and not host_matches(host, domains):
            await route.abort("blockedbyclient")
            return
        if is_ip_literal(host) and not address_allowed(host.strip("[]")):
            await route.abort("blockedbyclient")
            return
        if block_resources and request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return
        await route.continue_()

    return _route


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    html: str
    verdict: Verdict
    from_cache: bool = False
    retry_after_seconds: int | None = None


@dataclass
class SiteState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float | None = None  # time.monotonic()
    blocked_until: float | None = None  # time.monotonic()
    blocked_until_wall: str | None = None
    block_reason: str | None = None
    last_state: str = "unknown"
    last_checked: str | None = None
    last_reason: str | None = None

    def cooling_down(self) -> int | None:
        if self.blocked_until is None:
            return None
        remaining = self.blocked_until - time.monotonic()
        if remaining <= 0:
            self.blocked_until = None
            self.blocked_until_wall = None
            return None
        return int(remaining) + 1


class Fetcher:
    def __init__(self, config: SourcesConfig, *, headless: bool = True, profile_dir: Path | None = None,
                 cache_ttl_seconds: int = 300) -> None:
        self.config = config
        self.headless = headless
        self.profile_dir = profile_dir
        self.cache_ttl = max(0, cache_ttl_seconds)
        self._states: dict[str, SiteState] = {}
        self._cache: dict[str, tuple[float, FetchResult]] = {}
        self._init_lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._contexts: dict[str, Any] = {}
        self._persistent: Any = None
        self._http: Any = None
        self._guard: EgressGuard | None = None

    # -- state ---------------------------------------------------------------

    def state(self, key: str) -> SiteState:
        if key not in self._states:
            self._states[key] = SiteState()
        return self._states[key]

    @property
    def browser_running(self) -> bool:
        return self._browser is not None or self._persistent is not None

    def _remember(self, source: Source, verdict: Verdict) -> None:
        st = self.state(source.key)
        st.last_checked = utc_now_iso()
        st.last_reason = verdict.reason
        st.last_state = {
            "ok": "reachable",
            "blocked": "blocked",
            "login-required": "login-required",
        }.get(verdict.state, "unreachable")

    def _start_cooldown(self, source: Source, reason: str | None) -> int:
        timing = self.config.timing(source)
        st = self.state(source.key)
        st.blocked_until = time.monotonic() + timing.cooldown
        st.blocked_until_wall = datetime.fromtimestamp(time.time() + timing.cooldown, timezone.utc).replace(
            microsecond=0).isoformat()
        st.block_reason = reason
        log.warning("source %s blocked (%s); cooling down for %ss", source.key, reason, timing.cooldown)
        return timing.cooldown

    # -- cache ---------------------------------------------------------------

    def _cache_get(self, url: str) -> FetchResult | None:
        hit = self._cache.get(url)
        if not hit:
            return None
        expires, result = hit
        if expires < time.monotonic():
            self._cache.pop(url, None)
            return None
        return FetchResult(**{**result.__dict__, "from_cache": True})

    def _cache_put(self, url: str, result: FetchResult) -> None:
        if self.cache_ttl <= 0:
            return
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._cache.items() if exp < now]:
            self._cache.pop(key, None)
        while len(self._cache) >= CACHE_MAX_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[url] = (now + self.cache_ttl, result)

    # -- fetch ---------------------------------------------------------------

    async def fetch(self, source: Source, url: str, *, wait_for: str | None = None) -> FetchResult:
        st = self.state(source.key)
        remaining = st.cooling_down()
        if remaining:
            return FetchResult(url, url, None, "", Verdict("blocked", st.block_reason or "cooling_down"),
                               retry_after_seconds=remaining)
        cached = self._cache_get(url)
        if cached:
            return cached

        async with st.lock:
            remaining = st.cooling_down()
            if remaining:
                return FetchResult(url, url, None, "", Verdict("blocked", st.block_reason or "cooling_down"),
                                   retry_after_seconds=remaining)
            cached = self._cache_get(url)
            if cached:
                return cached

            timing = self.config.timing(source)
            if st.last_request is not None:
                delay = random.uniform(timing.min_delay, timing.max_delay)
                wait = st.last_request + delay - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                if source.fetch_mode == "http":
                    status, final_url, html = await self._fetch_http(url, timing.timeout)
                else:
                    status, final_url, html = await self._fetch_browser(source, url, wait_for, timing.timeout)
            except Exception as exc:  # network errors, timeouts, refused destinations
                st.last_request = time.monotonic()
                m = _NET_ERROR_RE.search(str(exc))
                verdict = Verdict("error", f"fetch_failed: {m.group(1) if m else type(exc).__name__}")
                log.warning("source %s: %s", source.key, verdict.reason)
                self._remember(source, verdict)
                return FetchResult(url, url, None, "", verdict)
            st.last_request = time.monotonic()

            if not host_matches(_host(final_url), source.domains):
                # Redirected off the source: discard the page, never parse it.
                verdict = Verdict("error", "redirected_off_source")
                log.warning("source %s: redirected off the source; result discarded", source.key)
                self._remember(source, verdict)
                return FetchResult(url, url, status, "", verdict)

            verdict = classify(status, final_url, html, extra_markers=self.config.defaults.block_markers
                               + source.block_markers, login_url_patterns=source.login_url_patterns)
            result = FetchResult(url, final_url, status, html, verdict)
            if verdict.state == "blocked":
                result.retry_after_seconds = self._start_cooldown(source, verdict.reason)
            elif verdict.state == "ok":
                self._cache_put(url, result)
            self._remember(source, verdict)
            log.info("source %s: %s %s", source.key, status, verdict.state)
            return result

    async def _ensure_guard(self) -> EgressGuard:
        if self._guard is None:
            guard = EgressGuard()
            await guard.start()
            self._guard = guard
        return self._guard

    def _proxy_settings(self, guard: EgressGuard) -> dict[str, str]:
        # "<-loopback>" removes Chromium's implicit proxy bypass for
        # localhost, so loopback targets also meet the guard.
        return {"server": guard.proxy_url, "bypass": "<-loopback>"}

    async def _fetch_http(self, url: str, timeout: float) -> tuple[int | None, str, str]:
        if self._http is None:
            from curl_cffi.requests import AsyncSession

            guard = await self._ensure_guard()
            # impersonate="chrome": present as a current desktop Chrome, the
            # same identity as the browser backend. No rotation. All traffic,
            # redirect hops included, goes through the egress guard.
            self._http = AsyncSession(impersonate="chrome",
                                      proxies={"http": guard.proxy_url, "https": guard.proxy_url})
        resp = await self._http.get(url, timeout=timeout, allow_redirects=True, max_redirects=5)
        return resp.status_code, str(resp.url), resp.text

    async def _ensure_browser(self) -> None:
        async with self._init_lock:
            if self._playwright is None:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
            if self._browser is None:
                guard = await self._ensure_guard()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless, proxy=self._proxy_settings(guard))
                log.info("browser started (headless=%s)", self.headless)

    async def _context_for(self, source: Source) -> Any:
        if source.login_required:
            if self.profile_dir is None:
                raise RuntimeError("login-gated source needs PRICE_SCANNER_PROFILE_DIR")
            async with self._init_lock:
                if self._playwright is None:
                    from playwright.async_api import async_playwright

                    self._playwright = await async_playwright().start()
                if self._persistent is None:
                    guard = await self._ensure_guard()
                    self._persistent = await self._playwright.chromium.launch_persistent_context(
                        str(self.profile_dir), headless=self.headless, proxy=self._proxy_settings(guard))
                    login_domains = [d for s in self.config.sources.values() if s.login_required
                                     for d in s.domains]
                    await self._persistent.route("**/*", make_route_handler(login_domains, False))
                    log.info("persistent browser profile opened")
            return self._persistent
        await self._ensure_browser()
        if source.key not in self._contexts:
            context = await self._browser.new_context(service_workers="block")
            await context.route("**/*", make_route_handler(source.domains, source.block_resources))
            self._contexts[source.key] = context
        return self._contexts[source.key]

    async def _fetch_browser(self, source: Source, url: str, wait_for: str | None,
                             timeout: float) -> tuple[int | None, str, str]:
        context = await self._context_for(source)
        page = await context.new_page()
        try:
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception as exc:
                # A challenge page can interrupt navigation (reloads, aborted
                # frames). If what rendered is a recognisable block page, report
                # that; anything else is a genuine fetch failure.
                html = ""
                try:
                    html = await page.content()
                except Exception:
                    pass
                if classify(None, page.url, html).state == "blocked":
                    return None, page.url, html
                log.warning("source %s: navigation error: %s", source.key, str(exc).splitlines()[0][:200])
                raise
            status = response.status if response else None
            if wait_for and status not in (401, 403, 429):
                try:
                    await page.wait_for_selector(wait_for, timeout=WAIT_FOR_TIMEOUT_MS)
                except Exception:
                    pass  # parse whatever rendered; an empty result is reported as such
            html = await page.content()
            return status, page.url, html
        finally:
            await page.close()

    async def close(self) -> None:
        for context in self._contexts.values():
            try:
                await context.close()
            except Exception:
                pass
        self._contexts.clear()
        for attr in ("_persistent", "_browser"):
            obj = getattr(self, attr)
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = None
        if self._guard is not None:
            await self._guard.close()
            self._guard = None
