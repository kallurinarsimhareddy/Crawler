"""Extract job postings from Gem job boards.

Gem hosts every customer's board on a shared domain, keyed by a slug::

    https://jobs.gem.com/<company-slug>

The board is a Next.js application that ships its postings as JSON in a
``__NEXT_DATA__`` block, so the shared crawler's embedded-state route is the
one that reads it. No link pattern is supplied: Gem addresses postings by
opaque identifier with no distinguishing path segment, and a pattern loose
enough to catch them would catch the board's navigation too.
"""

from __future__ import annotations

from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Gem"

#: Registrable domains Gem serves boards from.
_HOSTS: Final[tuple] = ("jobs.gem.com",)


def parse_board_url(career_url: str) -> str:
    """Validate a Gem board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Gem board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Gem board.

    Args:
        career_url: Any Gem board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Gem board.
        AdapterHttpError: If the board cannot be read.
    """
    return fetch_hosted_board(
        career_url,
        company_name,
        session,
        PLATFORM,
        hosts=_HOSTS,
    )
