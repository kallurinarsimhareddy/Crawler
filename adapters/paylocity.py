"""Extract job postings from Paylocity recruiting boards.

Paylocity serves each customer a server-rendered board at a fixed layout::

    https://recruiting.paylocity.com/recruiting/jobs/All/<guid>/<company-slug>

Every posting on it links to ``/recruiting/jobs/Details/<id>/<guid>/<slug>``,
which is specific enough to identify postings exactly rather than heuristically.
The board lists every opening on one page, so there is no pager to follow.

Paylocity has no public API. It is one of the most common systems in a US
mid-market company sheet, so the HTML board is read directly.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Paylocity"

#: Registrable domains Paylocity serves boards from.
_HOSTS: Final[tuple] = ("paylocity.com",)

#: A Paylocity posting URL. The id segment is what makes it a posting rather
#: than the board, a department filter or a share link.
_JOB_URL: Final[re.Pattern[str]] = re.compile(
    r"/recruiting/jobs/details/\d+", re.IGNORECASE
)


def parse_board_url(career_url: str) -> str:
    """Validate a Paylocity board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Paylocity board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Paylocity board.

    Args:
        career_url: Any Paylocity board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Paylocity board.
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
