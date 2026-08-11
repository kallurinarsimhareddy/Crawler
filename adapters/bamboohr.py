"""Extract job postings from BambooHR careers sites.

BambooHR backs every hosted careers page with a public JSON endpoint that
returns the whole board in one response::

    GET https://<subdomain>.bamboohr.com/careers/list

The subdomain is the leading label of the careers hostname, and each posting's
public URL is ``https://<subdomain>.bamboohr.com/careers/<id>``.
"""

from __future__ import annotations

from typing import Any, Dict, Final, List, Optional
from urllib.parse import parse_qs, urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_subdomain"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "BambooHR"

#: Hostname labels that are BambooHR's own, not a tenant's.
_RESERVED_LABELS: Final[frozenset] = frozenset({"www", "app", "api", "jobs", "careers"})


def parse_subdomain(career_url: str) -> str:
    """Read the tenant subdomain out of a BambooHR URL.

    Args:
        career_url: A BambooHR careers, embed or posting URL.

    Returns:
        The tenant subdomain.

    Raises:
        AdapterUrlError: If the URL names no tenant.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No BambooHR URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable BambooHR URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    label = host.split(".")[0]

    if "bamboohr" in host and label and label not in _RESERVED_LABELS:
        return label

    # Embedded boards on a company domain name the tenant in the query string.
    for key in ("subdomain", "company", "account"):
        values = parse_qs(parts.query).get(key)
        if values and values[0].strip():
            return values[0].strip()

    raise AdapterUrlError(
        f"No BambooHR tenant in {career_url!r} "
        "(expected something like https://<company>.bamboohr.com/careers)"
    )


def _location_of(posting: Dict[str, Any]) -> str:
    """Assemble a readable location from a BambooHR posting.

    Args:
        posting: One entry of the endpoint's ``result`` list.

    Returns:
        ``"City, State, Country"`` with missing parts omitted, or ``""``.
    """
    location = posting.get("location")
    if isinstance(location, str):
        return location.strip()
    if not isinstance(location, dict):
        return ""

    parts = [
        str(location.get("city") or "").strip(),
        str(location.get("state") or "").strip(),
        str(location.get("country") or "").strip(),
    ]
    return ", ".join(part for part in parts if part)


def _country_of(posting: Dict[str, Any]) -> str:
    """Read the country from a BambooHR posting, when it states one.

    Args:
        posting: One entry of the endpoint's ``result`` list.

    Returns:
        The country as published, or ``""``.
    """
    location = posting.get("location")
    if isinstance(location, dict):
        country = str(location.get("country") or "").strip()
        if country.lower() in {"united states", "usa", "us"}:
            return "United States"
        return country
    return ""


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a BambooHR careers site.

    Args:
        career_url: Any BambooHR careers or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every open posting, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names no tenant.
        AdapterHttpError: If the board cannot be read.
    """
    subdomain = parse_subdomain(career_url)
    board_url = f"https://{subdomain}.bamboohr.com/careers"
    api_url = f"{board_url}/list"

    logger.info("BambooHR: tenant {!r} for {!r}", subdomain, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        payload = get_json(http, api_url)
    finally:
        if owned:
            http.close()

    if not isinstance(payload, dict):
        raise AdapterHttpError(f"BambooHR returned {type(payload).__name__} for {api_url}")

    postings = payload.get("result")
    if not isinstance(postings, list):
        raise AdapterHttpError(f"BambooHR returned no result list for {api_url}")

    collected: List[Optional[Job]] = []
    for posting in postings:
        if not isinstance(posting, dict):
            continue

        posting_id = str(posting.get("id") or "").strip()
        collected.append(
            build_job(
                company_name=company_name,
                title=str(posting.get("jobOpeningName") or posting.get("title") or ""),
                job_url=f"{board_url}/{posting_id}" if posting_id else "",
                location=_location_of(posting),
                country=_country_of(posting),
                career_page_url=board_url,
                platform=PLATFORM,
            )
        )

    jobs = dedupe(collected)
    logger.success("BambooHR: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
