"""Extract job postings from isolved Hire boards.

isolved gives each customer a board on its own subdomain::

    https://<tenant>.isolvedhire.com/jobs/

Postings live at ``/jobs/<id>.html``. The board lists every opening on one page
and carries schema.org markup, so both the link pattern and the generic
structured-data route apply. isolved publishes no anonymous job API.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "isolved"

#: Registrable domains isolved serves boards from.
_HOSTS: Final[tuple] = ("isolvedhire.com", "myisolved.com", "isolvedhcm.com")

#: An isolved posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/jobs?/\d+", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate an isolved Hire board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not an isolved board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an isolved Hire board.

    Args:
        career_url: Any isolved board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not an isolved board.
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
