import pytest

from price_scanner.sources import SourcesConfig
from tests.conftest import EXAMPLE_SOURCES
from tests.test_service import FakeFetcher


async def test_delay_between_requests_to_same_site(monkeypatch):
    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("price_scanner.fetcher.asyncio.sleep", _sleep)
    config = SourcesConfig.model_validate(EXAMPLE_SOURCES)
    fetcher = FakeFetcher(config, {"market.example": (200, "<html></html>")})
    source = config.sources["example"]
    await fetcher.fetch(source, "https://market.example/a")
    await fetcher.fetch(source, "https://market.example/b")
    assert len(sleeps) == 1 and 1.5 < sleeps[0] <= 3.0


def test_min_delay_never_zero():
    data = {**EXAMPLE_SOURCES, "defaults": {**EXAMPLE_SOURCES["defaults"], "min_delay_seconds": 0, "max_delay_seconds": 0}}
    config = SourcesConfig.model_validate(data)
    timing = config.timing(config.sources["example"])
    assert timing.min_delay >= 1.0 and timing.max_delay >= timing.min_delay


async def test_network_error_is_reported_not_raised(monkeypatch):
    config = SourcesConfig.model_validate(EXAMPLE_SOURCES)
    fetcher = FakeFetcher(config, {})

    async def boom(url, timeout):
        raise TimeoutError("slow")

    monkeypatch.setattr(fetcher, "_fetch_http", boom)
    result = await fetcher.fetch(config.sources["example"], "https://market.example/a")
    assert result.verdict.state == "error" and "TimeoutError" in result.verdict.reason
