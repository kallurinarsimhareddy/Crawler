"""Extract job postings from TalentReef boards.

TalentReef — now part of Mitratech — hosts hourly-hiring boards for multi-site
employers::

    https://jobs.talentreef.com/<company-slug>

The board is rendered client-side and its postings arrive by XHR, so extraction
leans on the shared crawler's embedded-state and headless-browser routes rather
than on link patterns. TalentReef boards typically repeat one role across many
sites, and each of those has its own posting URL, so they survive deduplication
as the distinct openings they are.
"""

from __future__ import annotations

from typing import Final, List, Optional

import requests

from adapters._paginated_html import fetch_hosted_board, normalise_board_url
from models.job import Job

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "TalentReef"

#: Registrable domains TalentReef serves boards from.
_HOSTS: Final[tuple] = ("talentreef.com", "jobappnetwork.com")


def parse_board_url(career_url: str) -> str:
    """Validate a TalentReef board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a TalentReef board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a TalentReef board.

    Args:
        career_url: Any TalentReef board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not a TalentReef board.
        AdapterHttpError: If the board cannot be read.
    """
    return fetch_hosted_board(
        career_url,
        company_name,
        session,
        PLATFORM,
        hosts=_HOSTS,
    )
