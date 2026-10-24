"""The listing schema returned by ``search_marketplace`` and ``fetch_listing``."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Listing(BaseModel):
    """One listing, as shown on the page. Nothing is inferred or invented.

    ``posted_date`` is ``None`` and ``date_reliable`` is ``False`` whenever the
    page shows no date. ``flags`` carries markings the page itself shows (for
    example ``"replica"``); it is an addition to the spec's schema so such
    markings are surfaced as data, never hidden.
    """

    id: str
    site: str
    title: str
    price: float | None = None
    currency: str | None = None
    location: str | None = None
    url: str
    posted_date: str | None = None
    date_reliable: bool = False
    condition: str | None = None
    snippet: str | None = None
    fetched_at: str = Field(default_factory=utc_now_iso)
    flags: list[str] = Field(default_factory=list)
