"""Extract job postings from Zoho Recruit career sites.

Zoho Recruit gives each customer a career site on a regional Zoho domain::

    https://<tenant>.zohorecruit.com/jobs/Careers
    https://<tenant>.zohorecruit.eu/jobs/Careers

Postings live at ``/jobs/Careers/<id>/<slug>``. Zoho's REST API is
OAuth-authenticated and therefore unusable anonymously, so the public career
site is read instead — it is server-rendered and carries every opening.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Zoho Recruit"

#: Registrable domains Zoho Recruit serves career sites from.
_HOSTS: Final[tuple] = (
    "zohorecruit.com",
    "zohorecruit.eu",
    "zohorecruit.in",
    "zohorecruit.com.au",
)

#: A Zoho Recruit posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/jobs/careers/\d+", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate a Zoho Recruit career-site URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The career-site URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Zoho Recruit career site.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Zoho Recruit career site.

    Args:
        career_url: Any Zoho Recruit career-site or posting URL.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty if the site advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Zoho Recruit career site.
        AdapterHttpError: If the site cannot be read.
    """
    return fetch_hosted_board(
        career_url,
        company_name,
        session,
        PLATFORM,
        hosts=_HOSTS,
        job_url_pattern=_JOB_URL,
    )
