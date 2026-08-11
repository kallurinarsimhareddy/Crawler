"""Extract job postings from JazzHR boards.

JazzHR gives each customer a board on its own subdomain::

    https://<tenant>.applytojob.com/apply/

Postings link to ``/apply/<code>/<slug>``, and JazzHR embeds a schema.org
``JobPosting`` on every one of them — so when the link pattern misses, the
generic structured-data route in :func:`adapters.generic.extract_jobs` still
reads the board.

The public board lists every opening on one page; JazzHR's REST API is
key-authenticated and therefore not usable for anonymous crawling.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "JazzHR"

#: Registrable domains JazzHR serves boards from.
_HOSTS: Final[tuple] = ("applytojob.com", "jazzhr.com", "jazz.co")

#: A JazzHR posting URL: ``/apply/<code>/<slug>``. The trailing segment is what
#: separates a posting from the board root at ``/apply/``.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/apply/[A-Za-z0-9]{4,}/[\w\-]+", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate a JazzHR board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a JazzHR board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a JazzHR board.

    Args:
        career_url: Any JazzHR board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a JazzHR board.
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
