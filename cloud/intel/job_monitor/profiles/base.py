"""The site-profile contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

__all__ = ["ListingPage", "SiteProfile"]


@dataclass
class ListingPage:
    """What one listing page showed."""

    #: Raw job dicts in column names (``job_url``, ``title``, ``company_name``, ``location``,
    #: ``experience_level``, ``salary_budget``, ``keywords`` [list], ``remote``), page order.
    records: List[Dict[str, Any]] = field(default_factory=list)
    #: Absolute URL of the next page, or None when the listing has no further page.
    next_url: Optional[str] = None
    #: Job cards seen (including any that could not be read).
    cards: int = 0
    problems: List[str] = field(default_factory=list)


class SiteProfile:
    #: Registry key stored on the monitor (``job_source_monitors.profile``).
    name: str = ""
    #: The Source value written on every job.
    source_name: str = ""
    hosts: Tuple[str, ...] = ()
    #: Listings are newest-first, so an incremental run may stop at already-known pages.
    newest_first: bool = False
    #: How many days back the source's listing reaches (its own retention). A job whose
    #: listing date is older than that cannot be observed, so its absence from a full sweep
    #: is not evidence of closure (it becomes EXPIRED instead). None = the whole history.
    visible_window_days: Optional[int] = None
    #: HTTP statuses of a job's own URL that confirm the posting was removed at the source.
    gone_statuses: Tuple[int, ...] = (404, 410)

    def listing_url(self, source_url: str, filters: Mapping[str, Any]) -> str:
        """The first page to read for a monitor (filters already in the URL by default)."""
        return source_url

    def parse_listing(self, html: str, page_url: str) -> ListingPage:  # pragma: no cover - abstract
        raise NotImplementedError

    def canonical_key(self, canonical_url: str) -> Optional[str]:
        """A stabler identity than the canonical URL (e.g. drop a title slug); None keeps it."""
        return None
