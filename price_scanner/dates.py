"""Parse the dates marketplaces show ("Dzisiaj o 14:05", "18 października 2026",
"3 days ago", "2026-10-18").

:func:`parse_date` returns a :class:`ParsedDate` or ``None``. ``None`` means the
text holds no date we can read, and the caller must report
``posted_date: null``. Relative ages ("13 min.", "2 weeks ago") are marked
approximate, and an approximate date is never reported as reliable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ParsedDate:
    value: str  # ISO 8601: a date, or a datetime with offset when a time is shown
    approximate: bool


def _fold(text: str) -> str:
    """Lowercase and strip diacritics so "października" == "pazdziernika"."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("ł", "l").replace(" ", " ")


# Polish month names (nominative, genitive, abbreviations) and English ones,
# all diacritic-folded.
_MONTHS: dict[str, int] = {}
for number, names in enumerate(
    [
        "styczen stycznia sty january jan",
        "luty lutego lut february feb",
        "marzec marca mar march",
        "kwiecien kwietnia kwi april apr",
        "maj maja may",
        "czerwiec czerwca cze june jun",
        "lipiec lipca lip july jul",
        "sierpien sierpnia sie august aug",
        "wrzesien wrzesnia wrz september sep sept",
        "pazdziernik pazdziernika paz october oct",
        "listopad listopada lis november nov",
        "grudzien grudnia gru december dec",
    ],
    start=1,
):
    for name in names.split():
        _MONTHS[name] = number

_TODAY = ("dzisiaj", "dzis", "today")
_YESTERDAY = ("wczoraj", "yesterday")

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?")
_DMY_NUM_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
_DMY_TEXT_RE = re.compile(r"\b(\d{1,2})\s+([a-z]{3,12})\.?(?:\s+(\d{4}))?\b")

# Relative ages ("13 min.", "2 godziny temu", "3 days ago"). Unit stems are
# diacritic-folded; a missing count ("tydzien temu", "a week ago") means one.
_REL_RE = re.compile(
    r"(?:(\d+)\s*)?\b(sek|sec|min|godz|hour|hr|dni|dzien|day|tydz|tyg|week|mies|month|rok|lat|year)\w*"
)
_UNIT_SECONDS = {
    "sek": 1, "sec": 1,
    "min": 60,
    "godz": 3600, "hour": 3600, "hr": 3600,
    "dni": 86400, "dzien": 86400, "day": 86400,
    "tydz": 604800, "tyg": 604800, "week": 604800,
    "mies": 2592000, "month": 2592000,
    "rok": 31536000, "lat": 31536000, "year": 31536000,
}


def _with_time(day: date, text: str, tz: ZoneInfo) -> str:
    m = _TIME_RE.search(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour < 24 and minute < 60:
            return datetime.combine(day, time(hour, minute), tz).isoformat()
    return day.isoformat()


def parse_date(text: str | None, tz_name: str = "UTC", now: datetime | None = None) -> ParsedDate | None:
    if not text:
        return None
    tz = ZoneInfo(tz_name)
    now = now.astimezone(tz) if now else datetime.now(tz)
    folded = _fold(text).strip()
    if not folded:
        return None

    if any(re.search(rf"\b{w}\b", folded) for w in _TODAY):
        return ParsedDate(_with_time(now.date(), folded, tz), approximate=False)
    if any(re.search(rf"\b{w}\b", folded) for w in _YESTERDAY):
        return ParsedDate(_with_time(now.date() - timedelta(days=1), folded, tz), approximate=False)

    m = _ISO_RE.search(folded)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            day = date(y, mo, d)
        except ValueError:
            return None
        if m.group(4):
            return ParsedDate(datetime.combine(day, time(int(m.group(4)), int(m.group(5))), tz).isoformat(), False)
        return ParsedDate(day.isoformat(), False)

    m = _DMY_NUM_RE.search(folded)
    if m:
        try:
            day = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        return ParsedDate(_with_time(day, folded[m.end():], tz), False)

    for m in _DMY_TEXT_RE.finditer(folded):
        month = _MONTHS.get(m.group(2))
        if not month:
            continue
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            day = date(year, month, int(m.group(1)))
        except ValueError:
            return None
        if not m.group(3) and day > now.date():
            day = day.replace(year=year - 1)  # "18 Dec" seen in January
        return ParsedDate(_with_time(day, folded[m.end():], tz), False)

    m = _REL_RE.search(folded)
    if m:
        count = int(m.group(1)) if m.group(1) else 1
        seconds = count * _UNIT_SECONDS[m.group(2)]
        when = now - timedelta(seconds=seconds)
        if _UNIT_SECONDS[m.group(2)] < 86400:
            return ParsedDate(when.replace(microsecond=0).isoformat(), approximate=True)
        return ParsedDate(when.date().isoformat(), approximate=True)

    return None
