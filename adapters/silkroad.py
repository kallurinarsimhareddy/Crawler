"""Extract job postings from SilkRoad Recruiting boards.

SilkRoad's OpenHire boards are ColdFusion applications addressed entirely
through a ``fuseaction`` parameter::

    https://<tenant>.silkroad.com/epostings/index.cfm?fuseaction=app.jobsearch
    https://<tenant>.silkroad.com/epostings/index.cfm?fuseaction=app.jobinfo&jobid=<n>

``app.jobinfo`` is therefore what identifies a posting: every link on the board
shares a path and differs only in its query.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "SilkRoad"

#: Registrable domains SilkRoad serves boards from.
_HOSTS: Final[tuple] = ("silkroad.com", "silkroad-eng.com")

#: A SilkRoad posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(
    r"fuseaction=app\.jobinfo|jobid=\d+", re.IGNORECASE
)


def parse_board_url(career_url: str) -> str:
    """Validate a SilkRoad board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a SilkRoad board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a SilkRoad board.

    Args:
        career_url: Any SilkRoad board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a SilkRoad board.
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
