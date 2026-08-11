"""Extract job postings from Radancy TalentBrew career sites.

Radancy — formerly TMP Worldwide — builds bespoke career sites on its
TalentBrew platform. Large employers run them on their own domain, where only
the URL layout gives the platform away; smaller ones sit on Radancy's own::

    https://<tenant>.talentbrew.com/...

Postings live under ``/job/<id>``. TalentBrew sites are heavily customised, so
the shared crawler's fallbacks — structured data, embedded state, then the
headless browser — carry more of the load here than the link pattern does.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Radancy"

#: Registrable domains Radancy serves career sites from.
_HOSTS: Final[tuple] = ("radancy.com", "talentbrew.com", "tmpwebeng.com")

#: A TalentBrew posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/job/\d+", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate a Radancy career-site URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The career-site URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Radancy career site.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Radancy career site.

    Args:
        career_url: Any Radancy career-site or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty if the site advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Radancy career site.
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
