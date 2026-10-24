"""Named parser modules for sites the generic selector config cannot describe.

A source opts in with ``parser: <name>`` in ``config/sources.yaml``. A parser
module may define either or both of:

- ``parse_search(html, source, *, tz, flag_rules, limit) -> list[Listing]``
- ``parse_listing(html, url, source, *, tz, flag_rules) -> Listing | None``

Anything a parser does not define falls back to the generic extractor. Add a
new parser by creating a module here and listing it in ``PARSERS``.
"""

from __future__ import annotations

from types import ModuleType

from price_scanner.parsers import facebook

PARSERS: dict[str, ModuleType] = {
    "facebook": facebook,
}


def get_parser(name: str) -> ModuleType:
    try:
        return PARSERS[name]
    except KeyError:
        raise ValueError(f"unknown parser {name!r}; known: {sorted(PARSERS)}") from None
