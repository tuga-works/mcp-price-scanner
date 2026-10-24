from pathlib import Path

import pytest
import yaml

from price_scanner.sources import SourcesConfig, load_sources
from tests.conftest import REPO

EXAMPLE = REPO / "config" / "sources.example.yaml"


def test_example_config_loads():
    config, used = load_sources(EXAMPLE)
    assert used == EXAMPLE
    keys = set(config.sources)
    assert {"olx", "allegro", "allegro-lokalnie", "vinted", "facebook-marketplace", "loombard"} <= keys


def test_login_gated_sources_are_disabled_by_default():
    config, _ = load_sources(EXAMPLE)
    for source in config.sources.values():
        if source.login_required:
            assert source.enabled is False, source.key


def test_every_source_can_search():
    config, _ = load_sources(EXAMPLE)
    for source in config.sources.values():
        assert source.search is not None, source.key
        assert source.search.item or source.parser, source.key


def test_falls_back_to_example_when_operator_config_missing(tmp_path):
    (tmp_path / "sources.example.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    config, used = load_sources(tmp_path / "sources.yaml")
    assert used.name == "sources.example.yaml" and config.sources


def test_unknown_parser_rejected(tmp_path):
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["sources"]["olx"]["parser"] = "does-not-exist"
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_sources(path)


def test_unknown_keys_rejected():
    with pytest.raises(Exception):
        SourcesConfig.model_validate({"sources": {"x": {"name": "x", "base_url": "https://x.example/", "buy": True}}})


def test_plain_html_source_needs_no_code():
    config = SourcesConfig.model_validate({"sources": {"new-shop": {
        "name": "New shop", "enabled": True, "base_url": "https://new-shop.example/",
        "search": {"url": "https://new-shop.example/s?q={query_plus}", "item": ".product",
                   "fields": {"title": "h2", "url": {"selector": "a", "attr": "href"}, "price": ".price"}}}}})
    assert config.sources["new-shop"].key == "new-shop"
    assert config.source_for_url("https://www.new-shop.example/p/1").key == "new-shop"


def test_facebook_parser_on_synthetic_markup():
    from price_scanner.parsers import facebook

    config, _ = load_sources(EXAMPLE)
    source = config.sources["facebook-marketplace"]
    html = """<html><body>
      <a href="/marketplace/item/111/?ref=x"><div><span>120 zł</span></div><div><span>Desk lamp</span></div>
         <div><span>Example City</span></div></a>
      <a href="/marketplace/item/111/">duplicate</a>
      <a href="/marketplace/item/222/"><span>Free</span><span>Old chair</span></a>
    </body></html>"""
    listings = facebook.parse_search(html, source, tz="UTC", flag_rules=[], limit=10)
    assert [(l.id, l.title, l.price, l.location) for l in listings] == [
        ("111", "Desk lamp", 120.0, "Example City"),
        ("222", "Old chair", None, None),  # "Free" is the price line, not the title
    ]
    assert all(l.posted_date is None and not l.date_reliable for l in listings)
