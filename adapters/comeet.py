"""Extract job postings from Comeet boards.

Comeet's boards are embedded widgets backed by a documented public API::

    GET https://www.comeet.co/careers-api/2.0/company/<uid>/positions?token=<token>

Both the company uid and the token appear in the embed URL a company pastes
into its careers page, which is what tends to end up in a sourcing sheet. When
only the uid is present the uid is tried as the token as well, which is how
Comeet's own hosted board is addressed.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.discovery import text_of
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["API_ROOT", "PLATFORM", "fetch_jobs", "parse_company"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Comeet"

#: The public positions API.
API_ROOT: Final[str] = "https://www.comeet.co/careers-api/2.0/company"

#: A Comeet company uid looks like ``93.00A`` — digits, a dot, then a code.
_UID: Final[re.Pattern[str]] = re.compile(r"^\d+\.[0-9A-Z]{2,6}$", re.IGNORECASE)

#: Path segments that are the widget's plumbing, never the uid.
_NON_UID: Final[frozenset] = frozenset({"jobs", "careers", "careers-api", "company", "positions"})


def parse_company(career_url: str) -> Tuple[str, str]:
    """Read the company uid and API token out of a Comeet URL.

    Args:
        career_url: A Comeet board, embed or posting URL.

    Returns:
        ``(uid, token)``. The token falls back to the uid, which is how
        Comeet's own hosted boards authenticate.

    Raises:
        AdapterUrlError: If no company uid can be read from the URL.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Comeet URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Comeet URL: {career_url!r} ({exc})") from exc

    query = parse_qs(parts.query)
    token = (query.get("token") or [""])[0].strip()

    uid = ""
    for segment in (segment for segment in parts.path.split("/") if segment):
        if segment.lower() in _NON_UID:
            continue
        if _UID.match(segment):
            uid = segment
            break

    if not uid:
        uid = (query.get("uid") or query.get("company") or [""])[0].strip()

    if not uid:
        raise AdapterUrlError(
            f"No Comeet company uid in {career_url!r} "
            "(expected something like https://www.comeet.co/jobs/<name>/<uid>)"
        )

    return uid, token or uid


def _location_of(posting: Dict[str, Any]) -> Tuple[str, str]:
    """Read the location and country from a Comeet position.

    Args:
        posting: One entry of the positions list.

    Returns:
        ``(location, country)``, either of which may be ``""``.
    """
    node = posting.get("location")
    if not isinstance(node, dict):
        return text_of(node), ""

    country = text_of(node.get("country"))
    location = text_of(node.get("name")) or ", ".join(
        part
        for part in (text_of(node.get("city")), text_of(node.get("state")), country)
        if part
    )
    return location, country


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every position a Comeet company advertises.

    Args:
        career_url: Any Comeet board, embed or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every position, deduplicated. Empty if the company advertises nothing.

    Raises:
        AdapterUrlError: If ``career_url`` names no company.
        AdapterHttpError: If the API cannot be read.
    """
    uid, token = parse_company(career_url)
    api_url = f"{API_ROOT}/{uid}/positions"
    board_url = f"https://www.comeet.co/jobs/{uid}"

    logger.info("Comeet: company {!r} for {!r}", uid, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        payload = get_json(http, api_url, params={"token": token})
    finally:
        if owned:
            http.close()

    if isinstance(payload, dict):
        payload = payload.get("positions") or payload.get("data")
    if not isinstance(payload, list):
        raise AdapterHttpError(f"Comeet returned no positions list for {api_url}")

    collected = []
    for posting in payload:
        if not isinstance(posting, dict):
            continue
        location, country = _location_of(posting)
        collected.append(
            build_job(
                company_name=company_name,
                title=text_of(posting.get("name") or posting.get("title")),
                job_url=text_of(
                    posting.get("url_active_page") or posting.get("url_comeet_hosted_page")
                ),
                location=location,
                country=country,
                career_page_url=board_url,
                platform=PLATFORM,
            )
        )

    jobs = dedupe(collected)
    logger.success("Comeet: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
