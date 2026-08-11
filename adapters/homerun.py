"""Extract job postings from Homerun boards.

Homerun gives each customer a board on its own subdomain::

    https://<tenant>.homerun.co/

Boards are designed rather than templated, so posting URLs have no fixed shape
beyond sitting one segment below the board root. Homerun does publish a
schema.org ``JobPosting`` per opening, which is what the generic
structured-data route reads — so no link pattern is supplied, since guessing
one would only add noise the structured data already answers precisely.
"""

from __future__ import annotations

from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Homerun"

#: Registrable domains Homerun serves boards from.
_HOSTS: Final[tuple] = ("homerun.co",)


def parse_board_url(career_url: str) -> str:
    """Validate a Homerun board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Homerun board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Homerun board.

    Args:
        career_url: Any Homerun board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Homerun board.
        AdapterHttpError: If the board cannot be read.
    """
    return fetch_hosted_board(
        career_url,
        company_name,
        session,
        PLATFORM,
        hosts=_HOSTS,
    )
