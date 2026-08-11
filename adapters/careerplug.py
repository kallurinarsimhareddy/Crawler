"""Extract job postings from CareerPlug boards.

CareerPlug gives each customer a board on its own subdomain::

    https://<tenant>.careerplug.com/jobs

Postings live at ``/jobs/<id>/apps/new``, so the numeric id segment identifies
a posting. CareerPlug is common among franchise and multi-location employers,
whose boards are long and paged by a "next" link.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "CareerPlug"

#: Registrable domains CareerPlug serves boards from.
_HOSTS: Final[tuple] = ("careerplug.com",)

#: A CareerPlug posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/jobs/\d+", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate a CareerPlug board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a CareerPlug board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a CareerPlug board.

    Args:
        career_url: Any CareerPlug board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a CareerPlug board.
        AdapterHttpError: If the board cannot be read.
    """
    return fetch_hosted_board(
        career_url,
        company_name,
        session,
        PLATFORM,
        hosts=_HOSTS,
        job_url_pattern=_JOB_URL,
    )
