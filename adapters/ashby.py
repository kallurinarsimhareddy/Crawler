"""Extract job postings from Ashby job boards.

Ashby exposes every hosted board through a public Posting API that needs no
credentials and returns the whole board in one response::

    GET https://api.ashbyhq.com/posting-api/job-board/<slug>

The slug is the first path segment of ``jobs.ashbyhq.com/<slug>``.
"""

from __future__ import annotations

from typing import Any, Dict, Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_slug"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Ashby"

#: Path segments belonging to Ashby's routing rather than to a board.
_RESERVED: Final[frozenset] = frozenset({"posting-api", "job-board", "api", "embed"})


def parse_board_slug(career_url: str) -> str:
    """Read the board slug out of an Ashby URL.

    Args:
        career_url: An Ashby board or posting URL.

    Returns:
        The board slug.

    Raises:
        AdapterUrlError: If the URL names no board.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Ashby URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Ashby URL: {career_url!r} ({exc})") from exc

    for segment in (segment for segment in parts.path.split("/") if segment):
        if segment.lower() in _RESERVED:
            continue
        return segment

    raise AdapterUrlError(
        f"No Ashby board slug in {career_url!r} "
        "(expected something like https://jobs.ashbyhq.com/<company>)"
    )


def _location_of(posting: Dict[str, Any]) -> str:
    """Read the location from an Ashby posting.

    Ashby names one primary location and may list secondary ones; all are
    reported so a multi-site posting is not misrepresented as single-site.

    Args:
        posting: One posting from the API.

    Returns:
        The location string, or ``""``.
    """
    names = [str(posting.get("location") or "").strip()]

    secondary = posting.get("secondaryLocations")
    if isinstance(secondary, list):
        for entry in secondary:
            if isinstance(entry, dict):
                names.append(str(entry.get("location") or "").strip())
            elif isinstance(entry, str):
                names.append(entry.strip())

    unique = [name for index, name in enumerate(names) if name and name not in names[:index]]
    return " | ".join(unique)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an Ashby board.

    Args:
        career_url: Any Ashby board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names no board.
        AdapterHttpError: If the board cannot be read.
    """
    slug = parse_board_slug(career_url)
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    board_url = f"https://jobs.ashbyhq.com/{slug}"

    logger.info("Ashby: board {!r} for {!r}", slug, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        payload = get_json(http, api_url)
    finally:
        if owned:
            http.close()

    if not isinstance(payload, dict):
        raise AdapterHttpError(f"Ashby returned {type(payload).__name__} for {api_url}")

    postings = payload.get("jobs")
    if not isinstance(postings, list):
        raise AdapterHttpError(f"Ashby returned no jobs list for {api_url}")

    jobs = dedupe(
        build_job(
            company_name=company_name,
            title=str(posting.get("title") or ""),
            job_url=str(posting.get("jobUrl") or posting.get("applyUrl") or ""),
            location=_location_of(posting),
            career_page_url=board_url,
            platform=PLATFORM,
        )
        for posting in postings
        if isinstance(posting, dict)
    )

    logger.success("Ashby: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
