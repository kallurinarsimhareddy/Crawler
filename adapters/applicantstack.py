"""Extract job postings from ApplicantStack boards.

ApplicantStack gives each customer a board on its own subdomain, with every
page addressed under a short ``/x/`` prefix::

    https://<tenant>.applicantstack.com/x/openings

Postings live at ``/x/detail/<code>``, which is what separates a posting from
the listing, the application form and the tenant's other pages.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "ApplicantStack"

#: Registrable domains ApplicantStack serves boards from.
_HOSTS: Final[tuple] = ("applicantstack.com",)

#: An ApplicantStack posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/x/detail/", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate an ApplicantStack board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not an ApplicantStack board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an ApplicantStack board.

    Args:
        career_url: Any ApplicantStack board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not an ApplicantStack board.
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
