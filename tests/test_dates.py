from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from price_scanner.dates import parse_date

NOW = datetime(2026, 10, 24, 22, 40, tzinfo=ZoneInfo("Europe/Warsaw"))


@pytest.mark.parametrize(
    "text, value, approximate",
    [
        ("Dzisiaj o 22:18", "2026-10-24T22:18:00+02:00", False),
        ("Wczoraj o 09:05", "2026-10-23T09:05:00+02:00", False),
        ("18 października 2026", "2026-10-18", False),
        ("18 pazdziernika 2026", "2026-10-18", False),
        ("2 Oct 2026", "2026-10-02", False),
        ("2026-10-18", "2026-10-18", False),
        ("12.03.2026", "2026-03-12", False),
        ("today 08:00", "2026-10-24T08:00:00+02:00", False),
        ("13 min.", "2026-10-24T22:27:00+02:00", True),
        ("2 godziny temu", "2026-10-24T20:40:00+02:00", True),
        ("tydzień temu", "2026-10-17", True),
        ("3 days ago", "2026-10-21", True),
    ],
)
def test_parse(text, value, approximate):
    parsed = parse_date(text, "Europe/Warsaw", NOW)
    assert parsed is not None
    assert parsed.value == value
    assert parsed.approximate is approximate


@pytest.mark.parametrize("text", [None, "", "Example City", "Price on request"])
def test_no_date(text):
    assert parse_date(text, "Europe/Warsaw", NOW) is None


def test_month_without_year_in_future_rolls_back():
    parsed = parse_date("20 grudnia", "Europe/Warsaw", NOW)
    assert parsed.value == "2025-12-20"
