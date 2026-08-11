"""Extract job postings from Pinpoint boards.

Pinpoint publishes each tenant's live postings as unauthenticated JSON beside
the board itself, returning everything in one response::

    GET https://<tenant>.pinpointhq.com/postings.json

The tenant is the leftmost label of the board host. Pinpoint names the country
on every posting, so nothing has to be inferred from the location text.
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

__all__ = ["PLATFORM", "fetch_jobs", "parse_tenant"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Pinpoint"

#: Host labels that are Pinpoint's own plumbing, never a tenant.
_NON_TENANT: Final[frozenset] = frozenset({"www", "app", "api", "admin"})

#: Tenant labels are lowercase identifiers.
_TENANT: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]*$", re.IGNORECASE)


def parse_tenant(career_url: str) -> str:
    """Read the Pinpoint tenant out of a board or posting URL.

    Args:
        career_url: Any ``<tenant>.pinpointhq.com`` URL.

    Returns:
        The tenant identifier.

    Raises:
        AdapterUrlError: If no tenant can be read from the URL.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Pinpoint URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Pinpoint URL: {career_url!r} ({exc})") from exc

    if not host.endswith("pinpointhq.com"):
        raise AdapterUrlError(
            f"{career_url!r} is not a Pinpoint board (expected https://<tenant>.pinpointhq.com)"
        )

    label = host.split(".")[0]
    if label in _NON_TENANT or not _TENANT.match(label):
        raise AdapterUrlError(f"No Pinpoint tenant in {career_url!r}")

    return label


def _location_of(posting: Dict[str, Any]) -> tuple[str, str]:
    """Read the location and country from a Pinpoint posting.

    Args:
        posting: One entry of the board payload.

    Returns:
        ``(location, country)``, either of which may be ``""``.
    """
    node = posting.get("location")
    if isinstance(node, dict):
        country = text_of(node.get("country"))
        location = text_of(node.get("name")) or ", ".join(
            part
            for part in (text_of(node.get("city")), text_of(node.get("region")), country)
            if part
        )
        return location, country

    return text_of(node) or text_of(posting.get("location_name")), text_of(posting.get("country"))


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Pinpoint board.

    Args:
        career_url: Any Pinpoint board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` names no Pinpoint tenant.
        AdapterHttpError: If the board cannot be read.
    """
    tenant = parse_tenant(career_url)
    board_url = f"https://{tenant}.pinpointhq.com"
    api_url = f"{board_url}/postings.json"

    logger.info("Pinpoint: board {!r} for {!r}", tenant, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        payload = get_json(http, api_url)
    finally:
        if owned:
            http.close()

    postings: Any = payload
    if isinstance(payload, dict):
        postings = payload.get("data") or payload.get("postings") or payload.get("jobs")
    if not isinstance(postings, list):
        raise AdapterHttpError(f"Pinpoint returned no posting list for {api_url}")

    collected = []
    for posting in postings:
        if not isinstance(posting, dict):
            continue
        location, country = _location_of(posting)
        collected.append(
            build_job(
                company_name=company_name,
                title=text_of(posting.get("title") or posting.get("name")),
                job_url=text_of(posting.get("url") or posting.get("public_url")),
                location=location,
                country=country,
                career_page_url=board_url,
                platform=PLATFORM,
            )
        )

    jobs = dedupe(collected)
    logger.success("Pinpoint: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
