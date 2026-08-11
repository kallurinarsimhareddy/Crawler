"""Extract job postings from Fountain boards.

Fountain gives each customer a board on its own subdomain::

    https://<tenant>.fountain.com/

Postings live under ``/positions/<id>``. Fountain specialises in high-volume
hourly hiring, so a board is frequently the same handful of roles across many
sites; each has its own posting URL and survives deduplication accordingly.
The board hydrates client-side, so the shared crawler's embedded-state route
matters as much as the link pattern.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Fountain"

#: Registrable domains Fountain serves boards from.
_HOSTS: Final[tuple] = ("fountain.com",)

#: A Fountain posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/positions/[\w\-]+", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate a Fountain board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Fountain board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Fountain board.

    Args:
        career_url: Any Fountain board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Fountain board.
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
