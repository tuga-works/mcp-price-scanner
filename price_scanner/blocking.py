"""Recognise block pages, CAPTCHA walls and login walls.

A recognised block is reported, never worked around: the caller gets
``blocked`` and the source cools down. Markers are specific to challenge pages
(a site that merely embeds a bot-protection script is not "blocked").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_BLOCK_MARKERS = [
    "captcha-delivery.com",  # DataDome challenge
    "_cf_chl_opt",  # Cloudflare challenge
    "<title>just a moment",  # Cloudflare interstitial
    "attention required! | cloudflare",
    "px-captcha",  # PerimeterX / HUMAN
]

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TITLE_WORDS = ("captcha", "are you a robot", "czy jestes robotem", "czy jesteś robotem", "access denied")

BLOCK_STATUSES = {403: "http_403", 429: "http_429"}


@dataclass(frozen=True)
class Verdict:
    state: str  # "ok" | "blocked" | "login-required" | "error"
    reason: str | None = None


def classify(
    status: int | None,
    final_url: str,
    html: str,
    *,
    extra_markers: list[str] | None = None,
    login_url_patterns: list[str] | None = None,
) -> Verdict:
    if status in BLOCK_STATUSES:
        return Verdict("blocked", BLOCK_STATUSES[status])
    lowered = html[:200_000].lower()
    for marker in DEFAULT_BLOCK_MARKERS + [m.lower() for m in (extra_markers or [])]:
        if marker in lowered:
            return Verdict("blocked", "challenge_page")
    m = _TITLE_RE.search(html[:50_000])
    if m:
        title = m.group(1).strip().lower()
        if any(word in title for word in _TITLE_WORDS):
            return Verdict("blocked", "challenge_page")
    path = urlparse(final_url).path.lower() + ("?" + urlparse(final_url).query.lower() if urlparse(final_url).query else "")
    for pattern in login_url_patterns or []:
        if re.search(pattern, path):
            return Verdict("login-required", "login_wall")
    if status == 401:
        return Verdict("login-required", "http_401")
    if status is not None and status >= 400:
        return Verdict("error", f"http_{status}")
    return Verdict("ok")
