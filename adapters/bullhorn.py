"""Extract job postings from Bullhorn career portals.

Bullhorn career portals are single-page applications that address postings
through a URL fragment (``#/job/<id>``), so there is nothing in the served HTML
for a link-based extractor to find, and the fragment never reaches a server
either. Bullhorn's REST API is real and public, but every call needs a
``BhRestToken`` the portal mints for itself at runtime.

The route that works without impersonating that handshake is to let the portal
perform it: the shared crawler renders the board in headless Chromium and keeps
every JSON response it fetches, which includes the very ``search/JobOrder``
payload the portal renders from. No pattern is supplied, because none of the
static extraction routes can succeed here and claiming otherwise would only
produce navigation links.
"""

from __future__ import annotations

from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Bullhorn"

#: Registrable domains Bullhorn serves career portals from.
_HOSTS: Final[tuple] = ("bullhornstaffing.com", "bullhorn.com", "bullhorncareerportal.com")


def parse_board_url(career_url: str) -> str:
    """Validate a Bullhorn career-portal URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The portal URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Bullhorn career portal.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Bullhorn career portal.

    Args:
        career_url: Any Bullhorn career-portal URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty when the portal advertises
        nothing, or when the run forbids the browser — a Bullhorn portal
        cannot be read without one.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Bullhorn portal.
        AdapterHttpError: If the portal cannot be reached at all.
    """
    return fetch_hosted_board(
        career_url,
        company_name,
        session,
        PLATFORM,
        hosts=_HOSTS,
        follow_next=False,
    )
