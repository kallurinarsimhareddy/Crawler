"""Extract job postings from UKG Ready recruiting boards.

UKG Ready — the former Kronos Workforce Ready — serves boards from a shared
host keyed by a company number::

    https://secure4.saashr.com/ta/<company>.careers?CareersSearch=&lang=en-US

Scraping that page yields nothing: every anchor on it is ``#`` and the rows are
painted by React. The board instead calls an unauthenticated REST endpoint,
which is what this adapter reads — see :mod:`adapters._ta_recruitment`, shared
with Asure, which runs the same application on its own host.

This is a different product from UKG Pro, whose ``recruiting.ultipro.com``
boards :mod:`adapters.ultipro` handles; the two share a vendor but not a URL
layout, a page structure or an API.
"""

from __future__ import annotations

from typing import Final, List, Optional

import requests

from adapters._ta_recruitment import fetch_ta_board, parse_ta_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "UKG Ready"

#: Registrable domains UKG Ready serves boards from.
_HOSTS: Final[tuple] = ("saashr.com",)


def parse_board_url(career_url: str) -> str:
    """Validate a UKG Ready board URL and return its API origin.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board's scheme and host.

    Raises:
        AdapterUrlError: If the URL is not a UKG Ready board, or names no
            company.
    """
    return parse_ta_url(career_url, PLATFORM, _HOSTS)[0]


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every open requisition on a UKG Ready board.

    Args:
        career_url: Any UKG Ready board URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every requisition, deduplicated. Empty if the board advertises nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a UKG Ready board.
        AdapterHttpError: If the endpoint cannot be read.
    """
    return fetch_ta_board(career_url, company_name, session, PLATFORM, _HOSTS)
