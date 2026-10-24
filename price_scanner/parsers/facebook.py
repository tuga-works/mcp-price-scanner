"""Parser for Facebook Marketplace search results (login-gated, disabled by default).

Marketplace markup uses generated class names, so CSS selectors are not
stable. This parser instead follows the item links
(``/marketplace/item/<id>/``) and reads the visible text lines of each card:
the first line that holds a price is the price, the next non-price line is
the title, and the line after it is the location. Cards show no posting date,
so ``posted_date`` is always ``null``.

Read-only: the parser only reads the HTML of a results page that the
operator's own, manually created browser profile can see.

Status: written against the public structure of the page; not exercised in
the v1 test run because the source is disabled by default.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

from price_scanner.extract import build_listing, detect_currency, parse_price, strip_non_content
from price_scanner.schema import Listing, utc_now_iso
from price_scanner.sources import FlagRule, Source

_ITEM_RE = re.compile(r"/marketplace/item/(\d+)")
_FREE_RE = re.compile(r"^(free|za darmo|gratis|darmowe)$", re.IGNORECASE)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_search(html: str, source: Source, *, tz: str, flag_rules: list[FlagRule], limit: int) -> list[Listing]:
    tree = LexborHTMLParser(html)
    strip_non_content(tree)
    fetched_at = utc_now_iso()
    results: list[Listing] = []
    seen: set[str] = set()
    for link in tree.css('a[href*="/marketplace/item/"]'):
        href = link.attributes.get("href") or ""
        m = _ITEM_RE.search(href)
        if not m or m.group(1) in seen:
            continue
        lines = _lines(link.text(separator="\n", strip=True))
        price_text = next(
            (ln for ln in lines if (detect_currency(ln) and parse_price(ln) is not None) or _FREE_RE.match(ln)),
            None,
        )
        rest = [ln for ln in lines if ln != price_text]
        if not rest:
            continue
        values = {
            "id": m.group(1),
            "url": f"https://www.facebook.com/marketplace/item/{m.group(1)}/",
            "title": rest[0],
            "price": price_text,
            "location": rest[1] if len(rest) > 1 else None,
        }
        listing = build_listing(values, source, date_spec=None, dates_reliable=False, tz=tz,
                                flag_rules=flag_rules, fetched_at=fetched_at)
        if listing is None:
            continue
        seen.add(m.group(1))
        results.append(listing)
        if len(results) >= limit:
            break
    return results
