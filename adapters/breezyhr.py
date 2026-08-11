"""Extract job postings from Breezy HR boards.

Breezy publishes every board as unauthenticated JSON at a fixed path on the
tenant's own subdomain, returning the whole board in one response::

    GET https://<tenant>.breezy.hr/json

The tenant is the leftmost label of the board host. Breezy states the country
explicitly on each posting, so no country has to be inferred from the location
text.
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
PLATFORM: Final[str] = "BreezyHR"

#: A Breezy tenant is the subdomain label; these are the board's plumbing.
_NON_TENANT: Final[frozenset] = frozenset({"www", "app", "breezy", "api"})

#: Tenant labels are lowercase identifiers.
_TENANT: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]*$", re.IGNORECASE)


def parse_tenant(career_url: str) -> str:
    """Read the Breezy tenant out of a board or posting URL.

    Args:
        career_url: Any ``<tenant>.breezy.hr`` URL.

    Returns:
        The tenant identifier.

    Raises:
        AdapterUrlError: If no tenant can be read from the URL.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Breezy HR URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Breezy HR URL: {career_url!r} ({exc})") from exc

    if not host.endswith("breezy.hr"):
        raise AdapterUrlError(
            f"{career_url!r} is not a Breezy HR board (expected https://<tenant>.breezy.hr)"
        )

    label = host.split(".")[0]
    if label in _NON_TENANT or not _TENANT.match(label):
        raise AdapterUrlError(f"No Breezy HR tenant in {career_url!r}")

    return label


def _location_of(posting: Dict[str, Any]) -> tuple[str, str]:
    """Read the location and country from a Breezy posting.

    Args:
        posting: One entry of the board payload.

    Returns:
        ``(location, country)``. The country is ``""`` when Breezy does not
        name one, leaving it to be derived from the location text.
    """
    node = posting.get("location")
    if not isinstance(node, dict):
        return text_of(node), ""

    country = node.get("country")
    country_name = text_of(country) if country is not None else ""

    # Breezy pre-formats the full location; only assemble one when it does not.
    location = text_of(node.get("name"))
    if not location:
        parts = [
            text_of(node.get("city")),
            text_of(node.get("state")),
            country_name,
        ]
        location = ", ".join(part for part in parts if part)

    return location, country_name


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Breezy HR board.

    Args:
        career_url: Any Breezy board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` names no Breezy tenant.
        AdapterHttpError: If the board cannot be read.
    """
    tenant = parse_tenant(career_url)
    board_url = f"https://{tenant}.breezy.hr"
    api_url = f"{board_url}/json"

    logger.info("Breezy HR: board {!r} for {!r}", tenant, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        payload = get_json(http, api_url)
    finally:
        if owned:
            http.close()

    # Breezy answers with a bare list; a dict means an error page or a change.
    if isinstance(payload, dict):
        payload = payload.get("positions") or payload.get("jobs") or payload.get("data")
    if not isinstance(payload, list):
        raise AdapterHttpError(f"Breezy HR returned no posting list for {api_url}")

    collected = []
    for posting in payload:
        if not isinstance(posting, dict):
            continue
        location, country = _location_of(posting)
        collected.append(
            build_job(
                company_name=company_name,
                title=text_of(posting.get("name") or posting.get("title")),
                job_url=text_of(posting.get("url")) or f"{board_url}/p/{posting.get('id', '')}",
                location=location,
                country=country,
                career_page_url=board_url,
                platform=PLATFORM,
            )
        )

    jobs = dedupe(collected)
    logger.success("Breezy HR: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
