"""Generic, config-driven extraction of listings from HTML.

Search pages: every element matching ``search.item`` is one listing, and each
field comes from a :class:`~price_scanner.sources.FieldSpec`. Listing pages:
schema.org ``Product`` JSON-LD (when present) fills the fields, and the
configured selectors take precedence where they match.

Nothing here invents a value. A field the page does not show stays ``None``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse

from selectolax.lexbor import LexborHTMLParser, LexborNode

from price_scanner.dates import parse_date
from price_scanner.schema import Listing, utc_now_iso
from price_scanner.sources import FieldSpec, FlagRule, Source

_WS_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"\d[\d\s  .,']*")

# Currency marks as printed on pages -> ISO code. The mark must be on the page;
# a source's ``default_currency`` is used only when a price has no mark at all.
_CURRENCY_MARKS = [
    ("zł", "PLN"), ("zl", "PLN"), ("pln", "PLN"),
    ("€", "EUR"), ("eur", "EUR"),
    ("$", "USD"), ("usd", "USD"),
    ("£", "GBP"), ("gbp", "GBP"),
    ("kč", "CZK"), ("czk", "CZK"),
    ("ft", "HUF"), ("huf", "HUF"),
    ("lei", "RON"), ("ron", "RON"),
    ("sek", "SEK"), ("nok", "NOK"), ("dkk", "DKK"), ("chf", "CHF"),
]

SNIPPET_MAX = 300


def clean_text(text: str | None) -> str | None:
    if text is None:
        return None
    text = _WS_RE.sub(" ", text.replace(" ", " ")).strip()
    return text or None


def parse_price(text: str | None) -> float | None:
    """Turn "1 350,50 zł", "599.00 zł", "1.234" or "950" into a number.

    Returns ``None`` when the text carries no number ("Za darmo", "Zamienię").
    """
    if not text:
        return None
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    raw = re.sub(r"[\s  ']", "", m.group(0)).rstrip(".,")
    if not raw:
        return None
    if "," in raw and "." in raw:
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        raw = raw.replace(thousands, "").replace(decimal, ".")
    elif "," in raw or "." in raw:
        sep = "," if "," in raw else "."
        head, _, tail = raw.rpartition(sep)
        if raw.count(sep) == 1 and len(tail) != 3:
            raw = head.replace(sep, "") + "." + tail  # decimal separator
        else:
            raw = raw.replace(sep, "")  # thousands separators
    try:
        return float(raw)
    except ValueError:
        return None


def detect_currency(text: str | None) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for mark, code in _CURRENCY_MARKS:
        if mark.isalpha():
            if re.search(rf"(?<![a-z]){re.escape(mark)}(?![a-z])", lowered):
                return code
        elif mark in lowered:
            return code
    return None


def strip_non_content(tree: LexborHTMLParser) -> None:
    """Drop elements whose text is never listing content (inline CSS, scripts).

    Call after reading JSON-LD: it removes the script tags that carry it.
    """
    for node in tree.css("style, script, noscript, template"):
        node.decompose()


def _node_value(node: LexborNode, spec: FieldSpec) -> str | None:
    if spec.attr:
        value = node.attributes.get(spec.attr)
    else:
        value = node.text(separator=" ", strip=True)
    return value


def extract_field(root: LexborNode, spec: FieldSpec | None) -> str | None:
    """Return the first match that satisfies the spec (label and regex act as filters)."""
    if spec is None:
        return None
    if spec.value is not None:
        return spec.value
    nodes: Iterable[LexborNode] = root.css(spec.selector) if spec.selector else [root]
    for node in nodes:
        value = clean_text(_node_value(node, spec))
        if value is None:
            continue
        if spec.label:
            if not value.lower().startswith(spec.label.lower()):
                continue
            value = clean_text(value[len(spec.label) :])
            if value is None:
                continue
        if spec.regex:
            m = re.search(spec.regex, value)
            if not m:
                continue
            value = clean_text(m.group(1) if m.groups() else m.group(0))
            if value is None:
                continue
        return value
    return None


def canonical_url(url: str, base: str, strip_query: bool) -> str:
    absolute = urljoin(base, url)
    if not strip_query:
        return absolute
    parts = urlparse(absolute)
    return urlunparse((parts.scheme, parts.netloc, parts.path, "", "", ""))


def listing_id(url: str, source: Source, explicit: str | None, fallback: str | None = None) -> str:
    """Selector value, else ``id_pattern`` on the URL, else ``fallback``
    (JSON-LD sku on listing pages), else the last URL path segment."""
    if explicit:
        return explicit
    if source.id_pattern:
        m = re.search(source.id_pattern, url)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    if fallback:
        return fallback
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or url


def apply_flags(listing: Listing, rules: list[FlagRule]) -> None:
    for rule in rules:
        pattern = re.compile(rule.pattern, re.IGNORECASE)
        for field in rule.fields:
            value = getattr(listing, field, None)
            if isinstance(value, str) and pattern.search(value):
                if rule.name not in listing.flags:
                    listing.flags.append(rule.name)
                break


def resolve_date(raw: str | None, spec: FieldSpec | None, source_reliable: bool, tz: str) -> tuple[str | None, bool]:
    """Return ``(posted_date, date_reliable)`` for the raw date text."""
    if not raw:
        return None, False
    if spec is not None and spec.unreliable_if and re.search(spec.unreliable_if, raw, re.IGNORECASE):
        return None, False
    parsed = parse_date(raw, tz)
    if parsed is None:
        return None, False
    return parsed.value, bool(source_reliable and not parsed.approximate)


def build_listing(
    values: dict[str, str | None],
    source: Source,
    *,
    date_spec: FieldSpec | None,
    dates_reliable: bool,
    tz: str,
    flag_rules: list[FlagRule],
    fetched_at: str | None = None,
    price_value: float | None = None,
    fallback_id: str | None = None,
) -> Listing | None:
    title = clean_text(values.get("title"))
    url = values.get("url")
    if not title or not url:
        return None
    url = canonical_url(url, source.base_url, source.strip_query)
    price_text = values.get("price")
    price = price_value if price_value is not None else parse_price(price_text)
    currency = values.get("currency")
    if currency:
        currency = detect_currency(currency) or currency.strip().upper()
    else:
        currency = detect_currency(price_text)
    if currency is None and price is not None:
        currency = source.default_currency
    if price is None:
        currency = currency if values.get("currency") else None
    posted, reliable = resolve_date(values.get("date"), date_spec, dates_reliable, tz)
    snippet = clean_text(values.get("snippet"))
    if snippet and len(snippet) > SNIPPET_MAX:
        snippet = snippet[: SNIPPET_MAX - 1].rstrip() + "…"
    listing = Listing(
        id=listing_id(url, source, clean_text(values.get("id")), fallback_id),
        site=source.key,
        title=title,
        price=price,
        currency=currency,
        location=clean_text(values.get("location")),
        url=url,
        posted_date=posted,
        date_reliable=reliable if posted else False,
        condition=clean_text(values.get("condition")),
        snippet=snippet,
        fetched_at=fetched_at or utc_now_iso(),
    )
    apply_flags(listing, flag_rules)
    return listing


def parse_search_page(html: str, source: Source, *, tz: str, flag_rules: list[FlagRule], limit: int) -> list[Listing]:
    assert source.search is not None and source.search.item
    tree = LexborHTMLParser(html)
    strip_non_content(tree)
    fetched_at = utc_now_iso()
    results: list[Listing] = []
    seen: set[str] = set()
    for item in tree.css(source.search.item):
        values = {name: extract_field(item, spec) for name, spec in source.search.fields.items()}
        listing = build_listing(
            values,
            source,
            date_spec=source.search.fields.get("date"),
            dates_reliable=source.search.dates_reliable,
            tz=tz,
            flag_rules=flag_rules,
            fetched_at=fetched_at,
        )
        if listing is None or listing.url in seen:
            continue
        seen.add(listing.url)
        results.append(listing)
        if len(results) >= limit:
            break
    return results


# --- JSON-LD ---------------------------------------------------------------


def _iter_json_ld(tree: LexborHTMLParser) -> Iterable[dict[str, Any]]:
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                if "@graph" in obj:
                    stack.append(obj["@graph"])
                yield obj


def _type_is(obj: dict[str, Any], name: str) -> bool:
    t = obj.get("@type")
    return t == name or (isinstance(t, list) and name in t)


def _short_schema(value: Any) -> str | None:
    """"https://schema.org/UsedCondition" -> "UsedCondition"."""
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def json_ld_product(html_tree: LexborHTMLParser) -> dict[str, Any]:
    """Pull listing fields out of the first schema.org Product, if any."""
    for obj in _iter_json_ld(html_tree):
        if not _type_is(obj, "Product"):
            continue
        offers = obj.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        out: dict[str, Any] = {
            "title": obj.get("name"),
            "snippet": obj.get("description"),
            "id": obj.get("sku") or obj.get("productID"),
            "url": obj.get("url") or (offers.get("url") if isinstance(offers, dict) else None),
        }
        if isinstance(offers, dict):
            price = offers.get("price")
            if price is None and isinstance(offers.get("priceSpecification"), dict):
                price = offers["priceSpecification"].get("price")
            try:
                out["price_value"] = float(price) if price not in (None, "") else None
            except (TypeError, ValueError):
                out["price_value"] = parse_price(str(price))
            out["currency"] = offers.get("priceCurrency")
            area = offers.get("areaServed")
            if isinstance(area, dict):
                out["location"] = area.get("name")
            out["condition"] = _short_schema(offers.get("itemCondition"))
            # Offer.validFrom / priceValidUntil are not posting dates: ignored.
        out["condition"] = out.get("condition") or _short_schema(obj.get("itemCondition"))
        posted = obj.get("datePosted") or obj.get("datePublished")
        if isinstance(posted, str):
            out["date"] = posted
        return {k: v for k, v in out.items() if v not in (None, "")}
    return {}


def parse_listing_page(html: str, url: str, source: Source, *, tz: str, flag_rules: list[FlagRule]) -> Listing | None:
    tree = LexborHTMLParser(html)
    spec = source.listing
    root = tree.root
    values: dict[str, Any] = {}
    if spec.use_json_ld:
        values.update(json_ld_product(tree))
    price_value = values.pop("price_value", None)
    json_ld_id = values.pop("id", None)
    strip_non_content(tree)
    for name, field_spec in spec.fields.items():
        if root is None:
            break
        extracted = extract_field(root, field_spec)
        if extracted is not None:
            values[name] = extracted
            if name == "price":
                # The page's visible price wins, and so does its currency mark:
                # JSON-LD can state a different (e.g. the seller's) currency.
                price_value = None
                if "currency" not in spec.fields:
                    values.pop("currency", None)
    values["url"] = url  # the URL that was asked for, canonicalised below
    return build_listing(
        values,
        source,
        date_spec=spec.fields.get("date"),
        dates_reliable=spec.dates_reliable,
        tz=tz,
        flag_rules=flag_rules,
        price_value=price_value,
        fallback_id=str(json_ld_id) if json_ld_id is not None else None,
    )
