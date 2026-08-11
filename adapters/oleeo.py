"""Extract job postings from Oleeo boards.

Oleeo — formerly WCN, and still served from ``tal.net`` — hosts each customer's
board under a versioned path::

    https://<tenant>.tal.net/vx/lang-en-GB/mobile-0/brand-0/candidate/jobboard/vacancy/1/adv/

Postings live under ``/candidate/postings/<id>``. Oleeo estates vary in whether
the board is server-rendered or hydrated client-side, so the shared crawler's
fallbacks — embedded state, then the headless browser — matter more here than
on most vendors.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Oleeo"

#: Registrable domains Oleeo serves boards from.
_HOSTS: Final[tuple] = ("oleeo.com", "tal.net")

#: An Oleeo posting URL. Deliberately excludes ``/vacancy/<n>/``: that is the
#: *board's* own path, and every language switcher and pager on the page links
#: back to it. Matching it made a two-company estate report sixteen hundred
#: "jobs" with titles like "German".
_JOB_URL: Final[re.Pattern[str]] = re.compile(
    r"/candidate/(?:postings|so/pm)/\d+|/opp/\d+", re.IGNORECASE
)


def parse_board_url(career_url: str) -> str:
    """Validate an Oleeo board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not an Oleeo board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an Oleeo board.

    Args:
        career_url: Any Oleeo board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not an Oleeo board.
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
