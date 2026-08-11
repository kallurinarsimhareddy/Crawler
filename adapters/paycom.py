"""Extract job postings from Paycom applicant tracking boards.

Paycom hosts every customer's board on its own domain, keyed by a client key
in the query string::

    https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=<key>

Postings link to ``.../jobs/ViewJobDetails?job=<id>&clientkey=<key>``. The
board renders every opening on one page, so there is no pager to follow.

Paycom publishes no job API, so the board is read as HTML.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Paycom"

#: Registrable domains Paycom serves boards from.
_HOSTS: Final[tuple] = ("paycomonline.net", "paycomonline.com")

#: A Paycom posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"viewjobdetails", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate a Paycom board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Paycom board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Paycom board.

    Args:
        career_url: Any Paycom board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Paycom board.
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
