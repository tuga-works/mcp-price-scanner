from __future__ import annotations

from pathlib import Path

import pytest

from price_scanner.sources import SourcesConfig

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).parent.parent


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


EXAMPLE_SOURCES = {
    "defaults": {
        "min_delay_seconds": 2,
        "max_delay_seconds": 3,
        "cooldown_seconds": 3600,
        "timezone": "Europe/Warsaw",
        "flags": [{"name": "replica", "pattern": r"\brepli[kc]\w*"}],
    },
    "sources": {
        "example": {
            "name": "Example market",
            "enabled": True,
            "base_url": "https://market.example/",
            "domains": ["market.example", "shop.example"],
            "default_currency": "PLN",
            "fetch_mode": "http",
            "search": {
                "url": "https://market.example/search?q={query}",
                "sort": {"newest": "order=new"},
                "item": "div.card",
                "dates_reliable": True,
                "fields": {
                    "id": {"attr": "id"},
                    "title": "a.title",
                    "url": {"selector": "a.title", "attr": "href"},
                    "price": "p.price",
                    "location": {"selector": "p.meta", "regex": r"^(.*?)\s+-\s+"},
                    "date": {"selector": "p.meta", "regex": r"\s-\s+(.*)$", "unreliable_if": "refreshed"},
                    "condition": {"selector": "span.badge", "regex": "^(New|Used)$"},
                },
            },
            "listing": {
                "dates_reliable": True,
                "fields": {
                    "title": "h1",
                    "price": "div.price",
                    "date": {"selector": "p.posted", "regex": r"^Added\s+(.*)$"},
                    "condition": {"selector": "ul.params li", "label": "Condition:"},
                    "location": {"selector": "div.seller p.param", "label": "Location:"},
                },
            },
        },
        "gated": {
            "name": "Gated market",
            "enabled": True,
            "login_required": True,
            "base_url": "https://gated.example/",
            "search": {"url": "https://gated.example/s?q={query}", "item": "div.card",
                       "fields": {"title": "a", "url": {"selector": "a", "attr": "href"}}},
        },
        "off": {
            "name": "Disabled market",
            "enabled": False,
            "base_url": "https://off.example/",
            "search": {"url": "https://off.example/s?q={query}", "item": "div.card",
                       "fields": {"title": "a", "url": {"selector": "a", "attr": "href"}}},
        },
    },
}


@pytest.fixture
def config() -> SourcesConfig:
    return SourcesConfig.model_validate(EXAMPLE_SOURCES)
