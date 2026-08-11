"""Extract job postings from NeoGov boards.

NeoGov runs the public-sector job boards at ``governmentjobs.com`` and the
school-district equivalent at ``schooljobs.com``::

    https://www.governmentjobs.com/careers/<agency>

Postings live at ``/careers/<agency>/jobs/<id>/<slug>``, and long agency boards
page through a plain ``?page=<n>`` parameter, which is built directly rather
than followed as a link.
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
PLATFORM: Final[str] = "NeoGov"

#: Registrable domains NeoGov serves boards from.
_HOSTS: Final[tuple] = ("governmentjobs.com", "neogov.com", "schooljobs.com")

#: A NeoGov posting URL.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/jobs/\d+", re.IGNORECASE)

#: Hard stop on pages, so a state-wide board cannot dominate a run.
_MAX_PAGES: Final[int] = 40


def parse_board_url(career_url: str) -> str:
    """Validate a NeoGov board URL and strip any existing page parameter.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL without a page parameter, so paging can be driven here.

    Raises:
        AdapterUrlError: If the URL is not a NeoGov board.
    """
    normalised = normalise_board_url(career_url, PLATFORM, _HOSTS)
    parts = urlsplit(normalised)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a NeoGov board.

    Args:
        career_url: Any NeoGov board or posting URL for the agency.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a NeoGov board.
        AdapterHttpError: If the board cannot be read.
    """
    board_url = parse_board_url(career_url)

    return fetch_hosted_board(
        career_url,
        company_name,
        session,
        PLATFORM,
        hosts=_HOSTS,
        job_url_pattern=_JOB_URL,
        url_for_page=lambda index: f"{board_url}?page={index}",
        first_page=1,
        max_pages=_MAX_PAGES,
        board_url=board_url,
    )
