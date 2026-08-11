"""Extract job postings from Phenom People career sites.

Phenom hosts a company's careers site on the company's own domain, so the
platform is recognised by its URL layout rather than its host. The board is
client-side rendered, but the widget it renders from is reachable directly::

    GET https://<host>/widgets/?ddoKey=refineSearch&pageName=search-results
        &size=100&from=0&locale=en_US

The response nests the postings under ``refineSearch.data.jobs``. Phenom
estates vary in how much of that widget API they expose, so a tenant that does
not answer falls back to the shared HTML and headless-browser path — Phenom
sites embed a per-job ``JobPosting`` in their markup, which the generic
structured-data route reads.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from adapters._paginated_html import crawl_board
from models.job import Job
from utils.discovery import text_of
from utils.html import absolute_url
from utils.http import AdapterError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_origin"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Phenom"

#: Postings requested per widget call.
_PAGE_SIZE: Final[int] = 100

#: Hard stop on pages, so a board that never shortens cannot hang a run.
_MAX_PAGES: Final[int] = 40

#: Posting links on a rendered Phenom board.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/(?:job|jobs)/[\w%\-]+/\d+", re.IGNORECASE)


def parse_origin(career_url: str) -> str:
    """Read the site origin out of a Phenom career URL.

    Args:
        career_url: A Phenom career site or posting URL.

    Returns:
        The site's scheme and host.

    Raises:
        AdapterUrlError: If the URL has no usable host.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Phenom URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Phenom URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise AdapterUrlError(f"No host in Phenom URL {career_url!r}")

    return f"{parts.scheme or 'https'}://{host}"


def _postings_in(payload: Any) -> List[Dict[str, Any]]:
    """Read the postings out of a widget response.

    Args:
        payload: The decoded widget response.

    Returns:
        The posting objects, or an empty list if the response is not the shape
        the widget documents.
    """
    if not isinstance(payload, dict):
        return []

    node: Any = payload.get("refineSearch") or payload.get("eagerLoadRefineSearch") or payload
    if isinstance(node, dict):
        node = node.get("data", node)
    if isinstance(node, dict):
        node = node.get("jobs")

    return [item for item in node if isinstance(item, dict)] if isinstance(node, list) else []


def _location_of(posting: Dict[str, Any]) -> str:
    """Read the location from a Phenom posting.

    Args:
        posting: One entry of the widget's ``jobs`` list.

    Returns:
        The location text, or ``""``.
    """
    for key in ("cityStateCountry", "cityState", "location", "locations", "city"):
        found = text_of(posting.get(key))
        if found:
            return found
    return ""


def _jobs_from_widget(
    session: requests.Session, origin: str, company_name: str, board_url: str
) -> List[Job]:
    """Read the board through the Phenom widget API.

    Args:
        session: Session to use.
        origin: The site's scheme and host.
        company_name: Company as named in the input sheet.
        board_url: Board URL recorded on each job.

    Returns:
        The postings found. Empty when the widget does not answer usefully,
        which is the signal to fall back to HTML.
    """
    api_url = f"{origin}/widgets/"
    collected: List[Optional[Job]] = []

    try:
        for page in range(_MAX_PAGES):
            payload = get_json(
                session,
                api_url,
                params={
                    "ddoKey": "refineSearch",
                    "pageName": "search-results",
                    "size": _PAGE_SIZE,
                    "from": page * _PAGE_SIZE,
                    "locale": "en_US",
                    "sortBy": "Most recent",
                },
                headers={"Accept": "application/json"},
            )

            postings = _postings_in(payload)
            if not postings:
                break

            for posting in postings:
                collected.append(
                    build_job(
                        company_name=company_name,
                        title=text_of(posting.get("title") or posting.get("jobTitle")),
                        job_url=absolute_url(
                            origin,
                            text_of(
                                posting.get("jobSeoUrl")
                                or posting.get("applyUrl")
                                or posting.get("jobUrl")
                            ),
                        ),
                        location=_location_of(posting),
                        career_page_url=board_url,
                        platform=PLATFORM,
                    )
                )

            if len(postings) < _PAGE_SIZE:
                break
    except AdapterError as exc:
        logger.debug("Phenom: the widget API at {} did not answer ({})", api_url, exc)
        return []

    return dedupe(collected)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Phenom career site.

    Args:
        career_url: Any Phenom career site or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty if the site advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` has no usable host.
        AdapterHttpError: If neither the widget nor the board itself can be
            read.
    """
    origin = parse_origin(career_url)
    board_url = str(career_url or "").strip()

    logger.info("Phenom: site {} for {!r}", origin, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        jobs = _jobs_from_widget(http, origin, company_name, board_url)
        if not jobs:
            logger.debug("Phenom: falling back to the rendered board for {!r}", company_name)
            jobs = crawl_board(
                http,
                board_url,
                company_name,
                PLATFORM,
                job_url_pattern=_JOB_URL,
            )
    finally:
        if owned:
            http.close()

    logger.success("Phenom: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
