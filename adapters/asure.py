"""Extract job postings from Asure recruiting boards.

Asure's recruiting module runs the same ``/ta/`` application as UKG Ready, on
its own host::

    https://secure3.entertimeonline.com/ta/<company>.careers?CareersSearch=

As with UKG Ready, the rendered board is unscrapable — every anchor is ``#``
and the rows are painted by React — so the unauthenticated REST endpoint the
board itself calls is read instead. The mechanics live in
:mod:`adapters._ta_recruitment`, shared between the two products; only the
hosts and the reported platform label differ.
"""

from __future__ import annotations

from typing import Final, List, Optional

import requests

from adapters._ta_recruitment import fetch_ta_board, parse_ta_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Asure"

#: Registrable domains Asure serves boards from.
_HOSTS: Final[tuple] = ("entertimeonline.com", "asuresoftware.com")


def parse_board_url(career_url: str) -> str:
    """Validate an Asure board URL and return its API origin.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board's scheme and host.

    Raises:
        AdapterUrlError: If the URL is not an Asure board, or names no company.
    """
    return parse_ta_url(career_url, PLATFORM, _HOSTS)[0]


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every open requisition on an Asure board.

    Args:
        career_url: Any Asure board URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every requisition, deduplicated. Empty if the board advertises nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not an Asure board.
        AdapterHttpError: If the endpoint cannot be read.
    """
    return fetch_ta_board(career_url, company_name, session, PLATFORM, _HOSTS)
