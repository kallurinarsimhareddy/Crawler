"""Extract job postings from Recruitee career sites.

Recruitee mirrors every careers site at a public offers API that returns the
whole board in one response::

    GET https://<company>.recruitee.com/api/offers/

The company slug is the leading label of the careers hostname.
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
PLATFORM: Final[str] = "Recruitee"

#: Hostname labels that are Recruitee's own, not a tenant's.
_RESERVED_LABELS: Final[frozenset] = frozenset({"jobs", "careers", "www", "api", "app"})


def parse_company_slug(career_url: str) -> str:
    """Read the company slug out of a Recruitee URL.

    Args:
        career_url: A Recruitee careers or posting URL.

    Returns:
        The company slug.

    Raises:
        AdapterUrlError: If the URL names no company.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Recruitee URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Recruitee URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    label = host.split(".")[0]

    if label and label not in _RESERVED_LABELS and "recruitee.com" in host:
        return label

    # jobs.recruitee.com/<company>/... and custom domains keep it in the path.
    for segment in (segment for segment in parts.path.split("/") if segment):
        if segment.lower() in {"o", "api", "offers", "jobs"}:
            continue
        return segment

    raise AdapterUrlError(
        f"No Recruitee company in {career_url!r} "
        "(expected something like https://<company>.recruitee.com/)"
    )


def _location_of(offer: Dict[str, Any]) -> str:
    """Assemble a readable location from a Recruitee offer.

    Args:
        offer: One entry of the API's ``offers`` list.

    Returns:
        The published location, or a ``"City, Country"`` assembled from parts.
    """
    location = str(offer.get("location") or "").strip()
    if location:
        return location

    parts = [
        str(offer.get("city") or "").strip(),
        str(offer.get("state_name") or offer.get("state_code") or "").strip(),
        str(offer.get("country") or "").strip(),
    ]
    return ", ".join(part for part in parts if part)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Recruitee careers site.

    Args:
        career_url: Any Recruitee careers or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every published offer, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names no company.
        AdapterHttpError: If the site cannot be read.
    """
    slug = parse_company_slug(career_url)
    board_url = f"https://{slug}.recruitee.com/"
    api_url = f"https://{slug}.recruitee.com/api/offers/"

    logger.info("Recruitee: company {!r} for {!r}", slug, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        payload = get_json(http, api_url)
    finally:
        if owned:
            http.close()

    if not isinstance(payload, dict):
        raise AdapterHttpError(f"Recruitee returned {type(payload).__name__} for {api_url}")

    offers = payload.get("offers")
    if not isinstance(offers, list):
        raise AdapterHttpError(f"Recruitee returned no offers list for {api_url}")

    jobs = dedupe(
        build_job(
            company_name=company_name,
            title=str(offer.get("title") or ""),
            job_url=str(
                offer.get("careers_url") or offer.get("careers_apply_url") or offer.get("url") or ""
            ),
            location=_location_of(offer),
            career_page_url=board_url,
            platform=PLATFORM,
        )
        for offer in offers
        if isinstance(offer, dict)
    )

    logger.success("Recruitee: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
