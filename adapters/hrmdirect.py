"""Extract job postings from HRMDirect boards.

HRMDirect hosts a classic server-rendered board per customer::

    https://<tenant>.hrmdirect.com/employment/job-openings.php?search=true

Individual postings live at ``/employment/job-opening.php?req=<id>`` — note the
singular, which is the only thing separating a posting link from the listing it
sits on. The listing pages through a ``next`` link rather than a page number.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "HRMDirect"

#: Registrable domains HRMDirect serves boards from.
_HOSTS: Final[tuple] = ("hrmdirect.com",)

#: An HRMDirect posting URL. Singular "opening", unlike the listing page.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"job-opening\.php|jobid=|req=", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate an HRMDirect board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not an HRMDirect board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an HRMDirect board.

    Args:
        career_url: Any HRMDirect board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not an HRMDirect board.
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
