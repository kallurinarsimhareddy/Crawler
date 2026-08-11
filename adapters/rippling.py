"""Extract job postings from Rippling ATS boards.

Rippling serves every public board from one unauthenticated API keyed by the
board slug, returning the whole board in a single response::

    GET https://api.rippling.com/platform/api/ats/v1/board/<slug>/jobs

The slug is the first path segment of the public board URL
(``ats.rippling.com/<slug>/jobs``). Rippling also hosts boards under
``app.rippling.com/jobs/<slug>``, which carries the slug in the same position
once the ``jobs`` segment is skipped.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.discovery import text_of
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["API_ROOT", "PLATFORM", "fetch_jobs", "parse_board_slug"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Rippling"

#: The public board API.
API_ROOT: Final[str] = "https://api.rippling.com/platform/api/ats/v1/board"

#: Path segments that are the board's plumbing rather than the slug.
_NON_SLUG: Final[frozenset] = frozenset({"jobs", "job", "board", "boards", "ats", "careers"})

#: Board slugs are lowercase identifiers.
_SLUG: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)


def parse_board_slug(career_url: str) -> str:
    """Read the board slug out of a Rippling URL.

    Args:
        career_url: A Rippling board or posting URL.

    Returns:
        The board slug.

    Raises:
        AdapterUrlError: If no slug can be read from the URL.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Rippling URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Rippling URL: {career_url!r} ({exc})") from exc

    for segment in (segment for segment in parts.path.split("/") if segment):
        if segment.lower() in _NON_SLUG:
            continue
        if _SLUG.match(segment):
            return segment

    raise AdapterUrlError(
        f"No Rippling board slug in {career_url!r} "
        "(expected something like https://ats.rippling.com/<slug>/jobs)"
    )


def _location_of(posting: Dict[str, Any]) -> str:
    """Read the location from a Rippling posting.

    Args:
        posting: One entry of the board payload.

    Returns:
        The location text, or ``""``.
    """
    for key in ("workLocation", "location", "locations", "workLocations"):
        found = text_of(posting.get(key))
        if found:
            return found
    return ""


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Rippling board.

    Args:
        career_url: Any Rippling board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` names no board.
        AdapterHttpError: If the board cannot be read.
    """
    slug = parse_board_slug(career_url)
    api_url = f"{API_ROOT}/{slug}/jobs"
    board_url = f"https://ats.rippling.com/{slug}/jobs"

    logger.info("Rippling: board {!r} for {!r}", slug, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        payload = get_json(http, api_url)
    finally:
        if owned:
            http.close()

    if isinstance(payload, dict):
        payload = payload.get("jobs") or payload.get("results") or payload.get("data")
    if not isinstance(payload, list):
        raise AdapterHttpError(f"Rippling returned no posting list for {api_url}")

    jobs = dedupe(
        build_job(
            company_name=company_name,
            title=text_of(posting.get("name") or posting.get("title")),
            job_url=text_of(posting.get("url"))
            or f"{board_url}/{text_of(posting.get('uuid') or posting.get('id'))}",
            location=_location_of(posting),
            career_page_url=board_url,
            platform=PLATFORM,
        )
        for posting in payload
        if isinstance(posting, dict)
    )

    logger.success("Rippling: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
