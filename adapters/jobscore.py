"""Extract job postings from JobScore boards.

JobScore hosts every customer's board on a shared domain, keyed by a tenant
slug in the path::

    https://careers.jobscore.com/careers/<tenant>

Postings live one segment deeper, at ``/careers/<tenant>/jobs/<slug>-<code>``,
and JobScore publishes a schema.org ``JobPosting`` on each. The board lists
every opening on one page.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "JobScore"

#: Registrable domains JobScore serves boards from.
_HOSTS: Final[tuple] = ("jobscore.com",)

#: A JobScore posting URL: a ``jobs`` segment below the tenant's board.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/jobs/[\w\-]+", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate a JobScore board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a JobScore board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a JobScore board.

    Args:
        career_url: Any JobScore board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a JobScore board.
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
