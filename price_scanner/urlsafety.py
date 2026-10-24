"""URL and address checks that keep the bridge from being used as an SSRF relay.

``fetch_listing`` accepts URLs from the caller. Python's ``urllib`` and
Chromium's WHATWG parser disagree on edge cases (backslashes, userinfo,
trailing dots), so a URL is accepted only in a plain form both parsers read
the same way, and it is rebuilt from its parsed parts before anything fetches
it. Whatever host the browser ends up talking to is checked again at the
network layer by :mod:`price_scanner.egress`.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

# Backslash, "@", whitespace and control characters: no legitimate listing
# URL needs them unencoded, and each one is a known parser-confusion vector.
_FORBIDDEN_RE = re.compile(r"[\\@\s\x00-\x1f\x7f]")
ALLOWED_PORTS = {None, 80, 443}


class UnsafeURL(ValueError):
    """The URL is not in a form the bridge will fetch."""


def normalize_host(host: str | None) -> str:
    return (host or "").rstrip(".").lower()


def host_matches(host: str | None, domains: list[str]) -> bool:
    """Exact match or subdomain of one of ``domains`` (after normalisation)."""
    h = normalize_host(host)
    if not h:
        return False
    for domain in domains:
        d = normalize_host(domain)
        if d and (h == d or h.endswith("." + d)):
            return True
    return False


_NUMERIC_LABEL_RE = re.compile(r"(0x[0-9a-f]*|[0-9]+)")


def is_ip_literal(host: str | None) -> bool:
    """IP literals, including the WHATWG numeric forms browsers accept
    ("2130706433", "0x7f.1"): a host whose last label is a number is an IPv4
    address to Chromium."""
    h = normalize_host(host).strip("[]")
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return bool(h) and bool(_NUMERIC_LABEL_RE.fullmatch(h.rsplit(".", 1)[-1]))


def address_allowed(address: str) -> bool:
    """True only for public unicast addresses.

    Loopback, private (RFC 1918, ULA), link-local, carrier-grade NAT (which
    covers typical overlay/tailnet ranges), multicast, reserved and
    unspecified addresses are all refused, also when IPv4-mapped in IPv6.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def sanitize_listing_url(url: object) -> str:
    """Validate a caller-supplied URL and return the string to fetch.

    The result is rebuilt from scheme, host, port, path and query only: no
    userinfo, no fragment, host lowercased without a trailing dot. ``http`` is
    upgraded to ``https`` (the egress guard only tunnels TLS on port 443).
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise UnsafeURL("url must be a non-empty string of at most 2048 characters")
    if _FORBIDDEN_RE.search(url):
        raise UnsafeURL("url contains a backslash, '@', whitespace or a control character")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeURL("url could not be parsed") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UnsafeURL("only http(s) URLs are accepted")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("url must not contain credentials")
    host = normalize_host(parts.hostname)
    if not host:
        raise UnsafeURL("url has no host")
    if is_ip_literal(host):
        raise UnsafeURL("IP-address URLs are not accepted")
    if port not in ALLOWED_PORTS:
        raise UnsafeURL("only the default ports are accepted")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))
