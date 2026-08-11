"""Extract job postings from Paycor Recruiting boards.

Paycor Recruiting is the former Newton applicant tracking system, and both URL
estates are still live::

    https://recruiting.paycor.com/career/CareerHome.action?clientId=<id>
    https://<tenant>.newtonsoftware.com/career/CareerHome.action?clientId=<id>

Postings link to ``CareerHome`` siblings named ``JobIntroduction.action``. The
career home lists every opening on one page, so there is no pager to follow.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Paycor"

#: Registrable domains Paycor serves boards from, including the Newton estate.
_HOSTS: Final[tuple] = ("recruitingbypaycor.com", "paycor.com", "newtonsoftware.com")

#: A Paycor posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"jobintroduction\.action", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate a Paycor Recruiting board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Paycor board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Paycor Recruiting board.

    Args:
        career_url: Any Paycor board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Paycor board.
        AdapterHttpError: If the board cannot be read.
    """
    return fetch_hosted_board(
        career_url,
        company_name,
        session,
        PLATFORM,
        hosts=_HOSTS,
        job_url_pattern=_JOB_URL,
        follow_next=False,
    )
