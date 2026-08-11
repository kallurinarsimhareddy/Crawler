"""Extract job postings from ClearCompany boards.

ClearCompany gives each customer a board on its own subdomain::

    https://<tenant>.clearcompany.com/careers/jobs

Postings are addressed by a UUID under the same path, which is what
distinguishes them from the board's own department and location filters.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "ClearCompany"

#: Registrable domains ClearCompany serves boards from.
_HOSTS: Final[tuple] = ("clearcompany.com",)

#: A ClearCompany posting URL, identified by its UUID segment.
_JOB_URL: Final[re.Pattern[str]] = re.compile(
    r"/careers/jobs/[0-9a-f]{8}-[0-9a-f]{4}", re.IGNORECASE
)


def parse_board_url(career_url: str) -> str:
    """Validate a ClearCompany board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a ClearCompany board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a ClearCompany board.

    Args:
        career_url: Any ClearCompany board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a ClearCompany board.
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
