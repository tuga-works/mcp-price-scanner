import pytest

from price_scanner.extract import detect_currency, parse_listing_page, parse_price, parse_search_page
from tests.conftest import fixture_html


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1 350 zł", 1350.0),
        ("1 350,50 zł", 1350.5),
        ("599.00 zł", 599.0),
        ("421,94 zł", 421.94),
        ("1.234,56 €", 1234.56),
        ("1,234.56 $", 1234.56),
        ("12.500 zł", 12500.0),
        ("950 złdo negocjacji", 950.0),
        ("Za darmo", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_detect_currency():
    assert detect_currency("599 zł") == "PLN"
    assert detect_currency("12 €") == "EUR"
    assert detect_currency("12 EUR") == "EUR"
    assert detect_currency("Brazil 12") is None  # no "zl" inside words


def test_search_page(config):
    source = config.sources["example"]
    listings = parse_search_page(fixture_html("search_cards.html"), source, tz="Europe/Warsaw",
                                 flag_rules=config.defaults.flags, limit=10)
    assert [l.id for l in listings] == ["1001", "1002", "1003"]  # card without title skipped
    first, second, third = listings

    assert first.title == "Red bicycle"
    assert first.price == 1350.5 and first.currency == "PLN"
    assert first.url == "https://market.example/item/red-bicycle-1001.html"  # query stripped
    assert first.location == "Example City"
    assert first.condition == "Used"  # regex skips the "Free delivery" badge
    assert first.posted_date.startswith("20") and first.date_reliable is True

    # "Refreshed on ..." is not a posting date: never reported as one.
    assert second.posted_date is None and second.date_reliable is False
    # No number on the page: no price and no currency invented.
    assert second.price is None and second.currency is None
    assert second.flags == ["replica"]

    assert third.url == "https://shop.example/item/old-radio-1003.html"
    assert third.posted_date == "2026-10-18" and third.date_reliable is True


def test_search_limit(config):
    source = config.sources["example"]
    listings = parse_search_page(fixture_html("search_cards.html"), source, tz="UTC", flag_rules=[], limit=1)
    assert len(listings) == 1


def test_listing_without_date_is_null_and_unreliable(config):
    source = config.sources["example"]
    listing = parse_listing_page(fixture_html("listing_jsonld_nodate.html"),
                                 "https://market.example/item/old-radio-1003.html?x=1", source,
                                 tz="Europe/Warsaw", flag_rules=[])
    data = listing.model_dump()
    assert set(data) >= {"id", "site", "title", "price", "currency", "location", "url", "posted_date",
                         "date_reliable", "condition", "snippet", "fetched_at"}
    assert data["posted_date"] is None and data["date_reliable"] is False
    assert data["title"] == "Old radio"
    assert data["price"] == 599.0 and data["currency"] == "PLN"
    assert data["condition"] == "UsedCondition"
    assert data["id"] == "R-1003"  # JSON-LD sku
    assert data["location"] == "Example City, Main Street 1"
    assert data["snippet"] == "Works. Minor scratches."
    assert data["url"] == "https://market.example/item/old-radio-1003.html"


def test_listing_with_date(config):
    source = config.sources["example"]
    listing = parse_listing_page(fixture_html("listing_dated.html"), "https://market.example/item/red-bicycle-1001.html",
                                 source, tz="Europe/Warsaw", flag_rules=[])
    assert listing.posted_date == "2026-10-20T14:30:00+02:00"
    assert listing.date_reliable is True
    assert listing.price == 1350.5
    assert listing.condition == "Used"


def test_visible_price_currency_beats_json_ld_currency(config):
    source = config.sources["example"]
    html = """<html><head><script type="application/ld+json">
    {"@type": "Product", "name": "Camera", "offers": {"price": "100.00", "priceCurrency": "EUR"}}
    </script></head><body><h1>Camera</h1><div class="price">430,50 zł</div></body></html>"""
    listing = parse_listing_page(html, "https://market.example/item/cam", source, tz="UTC", flag_rules=[])
    assert (listing.price, listing.currency) == (430.5, "PLN")
