"""Extract job postings from Avature career sites.

Avature serves each customer a search page paged by a row offset rather than a
page number::

    https://<tenant>.avature.net/careers/SearchJobs?jobOffset=0
    https://<tenant>.avature.net/careers/SearchJobs?jobOffset=10

Postings live at ``/careers/JobDetail/<slug>/<id>``. Because the pager is an
offset, the page URL is built directly rather than by following a "next" link —
Avature renders that control with JavaScript on some tenants and not others.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional
from urllib.parse import urlsplit

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Avature"

#: Registrable domains Avature serves career sites from.
_HOSTS: Final[tuple] = ("avature.net",)

#: An Avature posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/careers/jobdetail/", re.IGNORECASE)

#: Postings per search page. Avature fixes this server-side.
_PAGE_SIZE: Final[int] = 10

#: Hard stop on pages, so a large estate cannot dominate a run.
_MAX_PAGES: Final[int] = 40


def parse_board_url(career_url: str) -> str:
    """Read the search URL out of an Avature career-site URL.

    Args:
        career_url: Any Avature career-site or posting URL.

    Returns:
        The tenant's ``/careers/SearchJobs`` URL.

    Raises:
        AdapterUrlError: If the URL is not an Avature career site.
    """
    normalised = normalise_board_url(career_url, PLATFORM, _HOSTS)
    parts = urlsplit(normalised)

    if "searchjobs" in parts.path.lower():
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    return f"{parts.scheme}://{parts.netloc}/careers/SearchJobs"


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an Avature career site.

    Args:
        career_url: Any Avature career-site or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty if the site advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not an Avature career site.
        AdapterHttpError: If the site cannot be read.
    """
    search_url = parse_board_url(career_url)

    return fetch_hosted_board(
        career_url,
        company_name,
        session,
        PLATFORM,
        hosts=_HOSTS,
        job_url_pattern=_JOB_URL,
        url_for_page=lambda index: f"{search_url}?jobOffset={index * _PAGE_SIZE}",
        first_page=0,
        max_pages=_MAX_PAGES,
        board_url=search_url,
    )
