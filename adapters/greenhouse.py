"""Extract job postings from Greenhouse job boards.

Greenhouse publishes every board through an unauthenticated API that returns
the whole board in one response, so no pagination is needed::

    GET https://boards-api.greenhouse.io/v1/boards/<token>/jobs

The board token is the path segment of the public board URL
(``boards.greenhouse.io/<token>``, ``job-boards.greenhouse.io/<token>``) or the
``for`` parameter of an embedded board. EU-hosted tenants are served from the
parallel ``.eu`` domains, which is derived from the board host.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional
from urllib.parse import parse_qs, urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_token"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Greenhouse"

#: Path segments that are part of the board's plumbing, never the token.
_NON_TOKEN_SEGMENTS: Final[frozenset] = frozenset(
    {"embed", "job_board", "jobs", "job", "boards", "v1", "api"}
)

#: A board token is a lowercase identifier; anything else is a page, not a board.
_TOKEN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]*$", re.IGNORECASE)


def parse_board_token(career_url: str) -> tuple[str, str]:
    """Read the board token and API host out of a Greenhouse URL.

    Args:
        career_url: A Greenhouse board URL, embedded board URL, or job URL.

    Returns:
        ``(token, api_host)`` — the board identifier and the API host serving
        it, which is the EU host for EU-hosted tenants.

    Raises:
        AdapterUrlError: If no board token can be read from the URL.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Greenhouse URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Greenhouse URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    api_host = "boards-api.eu.greenhouse.io" if ".eu." in host else "boards-api.greenhouse.io"

    # Embedded boards name their token in the query string.
    for key in ("for", "token", "board"):
        values = parse_qs(parts.query).get(key)
        if values and _TOKEN.match(values[0].strip()):
            return values[0].strip(), api_host

    for segment in (segment for segment in parts.path.split("/") if segment):
        if segment.lower() in _NON_TOKEN_SEGMENTS:
            continue
        if _TOKEN.match(segment):
            return segment, api_host

    # A vanity host such as "acme.greenhouse.io" carries the token as its label.
    label = host.split(".")[0]
    if label and label not in {"boards", "job-boards", "www"} and _TOKEN.match(label):
        return label, api_host

    raise AdapterUrlError(
        f"No Greenhouse board token in {career_url!r} "
        "(expected something like https://boards.greenhouse.io/<token>)"
    )


def _location_of(posting: Dict[str, Any]) -> str:
    """Read the location from a Greenhouse posting.

    Args:
        posting: One entry of the API's ``jobs`` list.

    Returns:
        The location name, or ``""``.
    """
    location = posting.get("location")
    if isinstance(location, dict):
        return str(location.get("name") or "").strip()
    if isinstance(location, str):
        return location.strip()

    offices = posting.get("offices")
    if isinstance(offices, list):
        names = [str(office.get("name") or "") for office in offices if isinstance(office, dict)]
        return ", ".join(name for name in names if name)

    return ""


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Greenhouse board.

    Args:
        career_url: Any Greenhouse board, embed or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` names no board.
        AdapterHttpError: If the board cannot be read.
    """
    token, api_host = parse_board_token(career_url)
    api_url = f"https://{api_host}/v1/boards/{token}/jobs"
    board_url = f"https://boards.greenhouse.io/{token}"

    logger.info("Greenhouse: board {!r} for {!r}", token, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        payload = get_json(http, api_url)
    finally:
        if owned:
            http.close()

    if not isinstance(payload, dict):
        raise AdapterHttpError(f"Greenhouse returned {type(payload).__name__} for {api_url}")

    postings = payload.get("jobs")
    if not isinstance(postings, list):
        raise AdapterHttpError(f"Greenhouse returned no jobs list for {api_url}")

    jobs = dedupe(
        build_job(
            company_name=company_name,
            title=str(posting.get("title") or ""),
            job_url=str(posting.get("absolute_url") or ""),
            location=_location_of(posting),
            career_page_url=board_url,
            platform=PLATFORM,
        )
        for posting in postings
        if isinstance(posting, dict)
    )

    logger.success("Greenhouse: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
