"""Source configuration: the YAML file that defines every marketplace.

Sources are data. A plain HTML source is added by writing a new entry in
``config/sources.yaml``; only a site that needs custom parsing names a parser
module from :mod:`price_scanner.parsers`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from price_scanner.urlsafety import host_matches, normalize_host

log = logging.getLogger(__name__)

MIN_DELAY_FLOOR = 1.0  # seconds; delays are never zero
SORT_KEYS = ("newest", "relevance", "price_asc", "price_desc")


class FieldSpec(BaseModel):
    """How to pull one value out of an element.

    - ``selector``: CSS selector relative to the item (omit to use the item).
    - ``attr``: read this attribute instead of the text.
    - ``label``: among the matches, take the one whose text starts with this
      label and strip the label ("Location: X" -> "X").
    - ``regex``: apply this regex; group 1 if present, else the whole match.
    - ``value``: a constant (for example a currency the page states once).
    - ``unreliable_if`` (dates only): if the raw text matches this regex the
      date is not a posting date (for example "refreshed on ..."): the
      listing gets ``posted_date: null, date_reliable: false``.
    """

    model_config = ConfigDict(extra="forbid")

    selector: str | None = None
    attr: str | None = None
    label: str | None = None
    regex: str | None = None
    value: str | None = None
    unreliable_if: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"selector": data}
        return data


FIELD_NAMES = ("id", "title", "url", "price", "currency", "location", "date", "condition", "snippet")


class FlagRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    pattern: str
    fields: list[str] = Field(default_factory=lambda: ["title", "snippet", "condition"])


class SearchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    sort: dict[str, str] = Field(default_factory=dict)
    item: str | None = None
    wait_for: str | None = None
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    dates_reliable: bool = False

    @field_validator("sort")
    @classmethod
    def _sort_keys(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - set(SORT_KEYS)
        if unknown:
            raise ValueError(f"unknown sort keys {sorted(unknown)}; allowed: {SORT_KEYS}")
        return value


class ListingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait_for: str | None = None
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    use_json_ld: bool = True
    dates_reliable: bool = False


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = ""
    name: str
    enabled: bool = False
    base_url: str
    domains: list[str] = Field(default_factory=list)
    login_required: bool = False
    fetch_mode: Literal["browser", "http"] = "browser"
    parser: str | None = None
    default_currency: str | None = None
    timezone: str | None = None
    min_delay_seconds: float | None = None
    max_delay_seconds: float | None = None
    cooldown_seconds: int | None = None
    timeout_seconds: float | None = None
    max_results: int | None = None
    block_resources: bool = True
    login_url_patterns: list[str] = Field(default_factory=list)
    block_markers: list[str] = Field(default_factory=list)
    id_pattern: str | None = None
    strip_query: bool = True
    health_url: str | None = None
    search: SearchSpec | None = None
    listing: ListingSpec = Field(default_factory=ListingSpec)
    flags: list[FlagRule] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _fill_domains(self) -> "Source":
        if not self.domains:
            host = normalize_host(urlparse(self.base_url).hostname)
            self.domains = [host.removeprefix("www.")] if host else []
        self.domains = [normalize_host(d) for d in self.domains]
        return self

    def matches_url(self, url: str) -> bool:
        """Host check only; callers fetch caller-supplied URLs only after
        :func:`price_scanner.urlsafety.sanitize_listing_url`."""
        try:
            host = urlparse(url).hostname
        except ValueError:
            return False
        return host_matches(host, self.domains)


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0
    cooldown_seconds: int = 3600
    timeout_seconds: float = 30.0
    max_results: int = 50
    timezone: str = "UTC"
    block_markers: list[str] = Field(default_factory=list)
    flags: list[FlagRule] = Field(default_factory=list)


class ResolvedTiming(BaseModel):
    min_delay: float
    max_delay: float
    cooldown: int
    timeout: float
    max_results: int
    timezone: str


class SourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: Defaults = Field(default_factory=Defaults)
    sources: dict[str, Source]

    @model_validator(mode="after")
    def _keys(self) -> "SourcesConfig":
        for key, source in self.sources.items():
            source.key = key
        return self

    def timing(self, source: Source) -> ResolvedTiming:
        d = self.defaults
        min_delay = source.min_delay_seconds if source.min_delay_seconds is not None else d.min_delay_seconds
        max_delay = source.max_delay_seconds if source.max_delay_seconds is not None else d.max_delay_seconds
        if min_delay < MIN_DELAY_FLOOR:
            log.warning("source %s: min delay raised to %.1fs (never zero)", source.key, MIN_DELAY_FLOOR)
            min_delay = MIN_DELAY_FLOOR
        max_delay = max(max_delay, min_delay)
        return ResolvedTiming(
            min_delay=min_delay,
            max_delay=max_delay,
            cooldown=source.cooldown_seconds if source.cooldown_seconds is not None else d.cooldown_seconds,
            timeout=source.timeout_seconds if source.timeout_seconds is not None else d.timeout_seconds,
            max_results=source.max_results if source.max_results is not None else d.max_results,
            timezone=source.timezone or d.timezone,
        )

    def source_for_url(self, url: str) -> Source | None:
        for source in self.sources.values():
            if source.matches_url(url):
                return source
        return None




def load_sources(path: Path) -> tuple[SourcesConfig, Path]:
    """Load the operator's config, falling back to the shipped example.

    Returns the parsed config and the path actually used.
    """
    used = path
    if not path.is_file():
        fallback = path.parent / "sources.example.yaml"
        if fallback.is_file():
            log.warning(
                "%s not found; using the shipped example %s. Copy it to %s and "
                "enable only the sources you need.",
                path,
                fallback,
                path,
            )
            used = fallback
        else:
            raise FileNotFoundError(f"source config not found: {path}")
    data = yaml.safe_load(used.read_text(encoding="utf-8")) or {}
    config = SourcesConfig.model_validate(data)
    from price_scanner.parsers import get_parser  # local import avoids a cycle

    for source in config.sources.values():
        if source.parser:
            get_parser(source.parser)  # raises on an unknown parser name
        if source.search is None and not source.parser:
            log.info("source %s has no search section: fetch_listing only", source.key)
    return config, used
