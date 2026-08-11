"""Extract job postings from Trakstar Hire boards.

Trakstar Hire is the former Recruiterbox, and both URL estates are still live::

    https://<tenant>.recruiterbox.com/jobs
    https://<tenant>.trakstar.com/jobs

Postings live one segment deeper, at ``/jobs/<code>``, and the board lists them
all on one page. The ``Platform`` column records the current product name so a
report reads the way a buyer would recognise it.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Trakstar Hire"

#: Registrable domains Trakstar Hire serves boards from.
_HOSTS: Final[tuple] = ("recruiterbox.com", "trakstar.com", "trakstarhire.com")

#: A posting URL: ``/jobs/<code>``, one segment below the board itself.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/jobs/[\w\-]+", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate a Trakstar Hire board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Trakstar Hire board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Trakstar Hire board.

    Args:
        career_url: Any Trakstar Hire or Recruiterbox URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Trakstar Hire board.
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
