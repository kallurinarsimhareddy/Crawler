"""Extract job postings from Lever job boards.

Lever mirrors every board at a public posting API that returns the whole board
in one response::

    GET https://api.lever.co/v0/postings/<company>?mode=json

The company slug is the first path segment of ``jobs.lever.co/<company>``.
EU-hosted tenants use the parallel ``api.eu.lever.co`` host, which is derived
from the board host.
"""

from __future__ import annotations

from typing import Any, Dict, Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_company_slug"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Lever"

#: Path segments that belong to Lever's own routing, not to a company.
_RESERVED: Final[frozenset] = frozenset({"v0", "postings", "api", "jobs"})


def parse_company_slug(career_url: str) -> tuple[str, str]:
    """Read the company slug and API host out of a Lever URL.

    Args:
        career_url: A Lever board or posting URL.

    Returns:
        ``(slug, api_host)``.

    Raises:
        AdapterUrlError: If the URL names no company.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Lever URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Lever URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    api_host = "api.eu.lever.co" if ".eu." in host else "api.lever.co"

    for segment in (segment for segment in parts.path.split("/") if segment):
        if segment.lower() in _RESERVED:
            continue
        return segment, api_host

    raise AdapterUrlError(
        f"No Lever company slug in {career_url!r} "
        "(expected something like https://jobs.lever.co/<company>)"
    )


def _location_of(posting: Dict[str, Any]) -> str:
    """Read the location from a Lever posting.

    Args:
        posting: One posting from the API.

    Returns:
        The location, or ``""``.
    """
    categories = posting.get("categories")
    if isinstance(categories, dict):
        location = str(categories.get("location") or "").strip()
        if location:
            return location

    workplace = posting.get("workplaceType")
    return str(workplace or "").strip()


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Lever board.

    Args:
        career_url: Any Lever board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names no company.
        AdapterHttpError: If the board cannot be read.
    """
    slug, api_host = parse_company_slug(career_url)
    api_url = f"https://{api_host}/v0/postings/{slug}"
    board_url = f"https://jobs.lever.co/{slug}"

    logger.info("Lever: board {!r} for {!r}", slug, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        payload = get_json(http, api_url, params={"mode": "json"})
    finally:
        if owned:
            http.close()

    if not isinstance(payload, list):
        raise AdapterHttpError(
            f"Lever returned {type(payload).__name__}, expected a list, for {api_url}"
        )

    jobs = dedupe(
        build_job(
            company_name=company_name,
            title=str(posting.get("text") or ""),
            job_url=str(posting.get("hostedUrl") or posting.get("applyUrl") or ""),
            location=_location_of(posting),
            career_page_url=board_url,
            platform=PLATFORM,
        )
        for posting in payload
        if isinstance(posting, dict)
    )

    logger.success("Lever: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
