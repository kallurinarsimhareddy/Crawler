"""Extract job postings from Join.com company pages.

Join hosts every company's openings on its own domain::

    https://join.com/companies/<company-slug>

Postings live one segment deeper, at ``/companies/<slug>/<id>-<job-slug>``.
Join is a Next.js application, so it ships its whole board as JSON in a
``__NEXT_DATA__`` block — which the shared crawler reads through
:mod:`utils.discovery` when the rendered links come up short.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Join.com"

#: Registrable domains Join serves company pages from.
_HOSTS: Final[tuple] = ("join.com",)

#: A Join posting URL: a numeric id below the company page.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/companies/[\w\-]+/\d+", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate a Join.com company-page URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The company page URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Join.com page.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Join.com company page.

    Args:
        career_url: Any Join.com company or posting URL.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty if the company advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Join.com page.
        AdapterHttpError: If the page cannot be read.
    """
    return fetch_hosted_board(
        career_url,
        company_name,
        session,
        PLATFORM,
        hosts=_HOSTS,
        job_url_pattern=_JOB_URL,
    )
