"""Extract job postings from Cornerstone OnDemand career sites.

A Cornerstone career site is a single-page application whose listings arrive
from a search API keyed by the career-site id in the board URL::

    https://<tenant>.csod.com/ux/ats/careersite/<siteId>/home?c=<tenant>
    GET  https://<tenant>.csod.com/services/x/career-site/v1/search?...

Cornerstone has shipped two generations of that endpoint and tenants are not
upgraded in lockstep, so both are tried before giving up. When neither answers
— some estates restrict the API to a session the SPA establishes — the board
falls back to the shared HTML and headless-browser path, which reads the
rendered result instead.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import requests
from loguru import logger

from adapters._paginated_html import crawl_board
from models.job import Job
from utils.discovery import text_of
from utils.http import AdapterError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_site"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Cornerstone"

#: Postings requested per call.
_PAGE_SIZE: Final[int] = 100

#: Hard stop on pages, so a board that never shortens cannot hang a run.
_MAX_PAGES: Final[int] = 40

#: ``/ux/ats/careersite/<siteId>/home`` — the site id is the segment after
#: ``careersite``, and is the only thing the search API needs.
_SITE_ID: Final[re.Pattern[str]] = re.compile(r"/careersite/(\d+)", re.IGNORECASE)

#: The two generations of the search endpoint, newest first.
_SEARCH_PATHS: Final[Tuple[str, ...]] = (
    "/services/x/career-site/v2/search",
    "/services/x/career-site/v1/search",
)

#: Posting links on a rendered Cornerstone board.
_JOB_URL: Final[re.Pattern[str]] = re.compile(r"/ux/ats/careersite/\d+/job/\d+", re.IGNORECASE)


def parse_site(career_url: str) -> Tuple[str, str]:
    """Read the board origin and career-site id out of a Cornerstone URL.

    Args:
        career_url: A Cornerstone career site or posting URL.

    Returns:
        ``(origin, site_id)``. ``site_id`` is ``""`` when the URL does not name
        one, in which case only the HTML path is available.

    Raises:
        AdapterUrlError: If the URL has no usable host.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Cornerstone URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Cornerstone URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise AdapterUrlError(f"No host in Cornerstone URL {career_url!r}")

    match = _SITE_ID.search(parts.path)
    site_id = match.group(1) if match else (parse_qs(parts.query).get("site") or [""])[0].strip()

    return f"{parts.scheme or 'https'}://{host}", site_id


def _location_of(posting: Dict[str, Any]) -> str:
    """Read the location from a Cornerstone requisition.

    Args:
        posting: One entry of the search response.

    Returns:
        The location text, or ``""``.
    """
    for key in ("locations", "location", "displayLocation", "locationName", "city"):
        found = text_of(posting.get(key))
        if found:
            return found
    return ""


def _jobs_from_api(
    session: requests.Session,
    origin: str,
    site_id: str,
    company_name: str,
    board_url: str,
) -> List[Job]:
    """Read the board through the search API.

    Args:
        session: Session to use.
        origin: The board's scheme and host.
        site_id: The career-site id.
        company_name: Company as named in the input sheet.
        board_url: Board URL recorded on each job.

    Returns:
        The postings found. Empty when neither endpoint generation answers
        usefully, which is the signal to fall back to HTML.
    """
    for path in _SEARCH_PATHS:
        api_url = f"{origin}{path}"
        collected: List[Optional[Job]] = []

        try:
            for page in range(_MAX_PAGES):
                payload = get_json(
                    session,
                    api_url,
                    params={
                        "careerSiteId": site_id,
                        "cultureId": 1,
                        "pageNumber": page + 1,
                        "pageSize": _PAGE_SIZE,
                        "sortOrder": "1",
                    },
                    headers={"Accept": "application/json"},
                )

                body = payload.get("data") if isinstance(payload, dict) else None
                requisitions = None
                for holder in (body, payload):
                    if isinstance(holder, dict):
                        requisitions = (
                            holder.get("requisitions")
                            or holder.get("jobs")
                            or holder.get("results")
                        )
                        if isinstance(requisitions, list):
                            break

                if not isinstance(requisitions, list) or not requisitions:
                    break

                for posting in requisitions:
                    if not isinstance(posting, dict):
                        continue
                    identifier = text_of(posting.get("requisitionId") or posting.get("id"))
                    collected.append(
                        build_job(
                            company_name=company_name,
                            title=text_of(
                                posting.get("displayJobTitle")
                                or posting.get("jobTitle")
                                or posting.get("title")
                            ),
                            job_url=(
                                f"{origin}/ux/ats/careersite/{site_id}/job/{identifier}"
                                if identifier
                                else ""
                            ),
                            location=_location_of(posting),
                            career_page_url=board_url,
                            platform=PLATFORM,
                        )
                    )

                if len(requisitions) < _PAGE_SIZE:
                    break
        except AdapterError as exc:
            logger.debug("Cornerstone: {} did not answer ({})", api_url, exc)
            continue

        jobs = dedupe(collected)
        if jobs:
            logger.debug("Cornerstone: {} job(s) via {}", len(jobs), path)
            return jobs

    return []


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Cornerstone career site.

    The search API is tried first, in both of its generations. A tenant that
    restricts it falls back to reading the board as HTML, and then — if the run
    allows it — to rendering it in a headless browser.

    Args:
        career_url: Any Cornerstone career site or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` has no usable host.
        AdapterHttpError: If neither the API nor the board itself can be read.
    """
    origin, site_id = parse_site(career_url)
    board_url = (
        f"{origin}/ux/ats/careersite/{site_id}/home" if site_id else str(career_url).strip()
    )

    logger.info("Cornerstone: site {!r} at {} for {!r}", site_id or "?", origin, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        if site_id:
            jobs = _jobs_from_api(http, origin, site_id, company_name, board_url)
            if jobs:
                logger.success("Cornerstone: {} job(s) for {!r}", len(jobs), company_name)
                return jobs

        logger.debug("Cornerstone: falling back to the rendered board for {!r}", company_name)
        jobs = crawl_board(
            http,
            board_url,
            company_name,
            PLATFORM,
            job_url_pattern=_JOB_URL,
            follow_next=False,
        )
    finally:
        if owned:
            http.close()

    logger.success("Cornerstone: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
