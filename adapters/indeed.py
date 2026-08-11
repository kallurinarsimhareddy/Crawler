"""Extract job postings from Indeed company pages.

Some sheets record an Indeed company page rather than the employer's own board::

    https://www.indeed.com/cmp/<company>/jobs

Indeed is an aggregator rather than an applicant tracking system, and it
defends itself against automated reading more aggressively than any ATS does.
This adapter exists so that those rows are attempted and reported honestly
rather than silently landing in the generic bucket — but a page that answers
with an interstitial will produce nothing, and that outcome is recorded as a
technical failure rather than as an empty board.

Postings are identified by Indeed's ``viewjob`` and ``rc/clk`` link shapes.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Indeed"

#: Registrable domains Indeed serves company pages from.
_HOSTS: Final[tuple] = ("indeed.com", "indeed.jobs")

#: An Indeed posting link, in both of the shapes it publishes.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/viewjob\?|/rc/clk\?|/job/", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate an Indeed company-page URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The company page URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not an Indeed page.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an Indeed company page.

    Args:
        career_url: Any Indeed company or posting URL.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty if the page shows none.

    Raises:
        AdapterUrlError: If ``career_url`` is not an Indeed page.
        AdapterHttpError: If the page cannot be read, which for Indeed usually
            means an anti-bot interstitial rather than an outage.
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
