"""Extract job postings from Oracle Cloud Recruiting (Candidate Experience).

Oracle's candidate-experience sites are React apps served from a customer's
Fusion pod, backed by a public REST resource that takes the site number from the
page URL::

    GET https://<pod>/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&finder=findReqs;siteNumber=<site>,limit=200,offset=0

The site number is the path segment after ``/sites/``. The response nests the
postings one level down, under ``items[0].requisitionList``, alongside the
board's ``TotalJobsCount``.
"""

from __future__ import annotations

from typing import Any, Dict, Final, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_site"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Oracle"

#: Requisitions requested per call.
_PAGE_SIZE: Final[int] = 200

#: Hard stop on pagination, far above any real site.
_MAX_PAGES: Final[int] = 100


def parse_site(career_url: str) -> Tuple[str, str]:
    """Read the Fusion host and candidate-experience site number out of a URL.

    Args:
        career_url: An Oracle candidate-experience URL.

    Returns:
        ``(host, site_number)``.

    Raises:
        AdapterUrlError: If the URL names no site.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Oracle URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Oracle URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise AdapterUrlError(f"Oracle URL has no hostname: {career_url!r}")

    segments = [segment for segment in parts.path.split("/") if segment]
    for index, segment in enumerate(segments):
        if segment.lower() == "sites" and index + 1 < len(segments):
            return host, segments[index + 1]

    raise AdapterUrlError(
        f"No Oracle site number in {career_url!r} "
        "(expected .../hcmUI/CandidateExperience/en/sites/<site>/...)"
    )


def _requisition_pages(payload: Any, api_url: str) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Unwrap Oracle's nested response.

    Args:
        payload: The decoded response.
        api_url: URL, for the error message.

    Returns:
        ``(requisitions, total)`` where ``total`` is the board's reported count
        or ``None`` when it does not state one.

    Raises:
        AdapterHttpError: If the response is not shaped as expected.
    """
    if not isinstance(payload, dict):
        raise AdapterHttpError(f"Oracle returned {type(payload).__name__} for {api_url}")

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return [], None

    first = items[0]
    if not isinstance(first, dict):
        raise AdapterHttpError(f"Oracle returned an unreadable items entry for {api_url}")

    requisitions = first.get("requisitionList")
    if not isinstance(requisitions, list):
        return [], None

    raw_total = first.get("TotalJobsCount")
    total = raw_total if isinstance(raw_total, int) and raw_total >= 0 else None

    return [item for item in requisitions if isinstance(item, dict)], total


def _location_of(requisition: Dict[str, Any]) -> str:
    """Read the location from an Oracle requisition.

    Args:
        requisition: One entry of ``requisitionList``.

    Returns:
        The primary location, noting any secondary ones.
    """
    primary = str(
        requisition.get("PrimaryLocation") or requisition.get("Location") or ""
    ).strip()

    secondary = requisition.get("secondaryLocations")
    if isinstance(secondary, list) and secondary and primary:
        return f"{primary} (+{len(secondary)} more)"

    return primary


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every requisition on an Oracle candidate-experience site.

    Args:
        career_url: An Oracle candidate-experience URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every open requisition, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names no site.
        AdapterHttpError: If the site cannot be read.
    """
    host, site = parse_site(career_url)
    api_url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    board_url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/requisitions"

    logger.info("Oracle: site {!r} on {} for {!r}", site, host, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    collected: List[Optional[Job]] = []
    offset = 0
    total: Optional[int] = None

    try:
        for page in range(1, _MAX_PAGES + 1):
            finder = (
                f"findReqs;siteNumber={site},limit={_PAGE_SIZE},offset={offset},"
                "sortBy=POSTING_DATES_DESC"
            )
            payload = get_json(
                http,
                api_url,
                params={
                    "onlyData": "true",
                    "expand": "requisitionList.secondaryLocations",
                    "finder": finder,
                },
                headers={"Referer": board_url},
            )

            requisitions, reported = _requisition_pages(payload, api_url)
            if total is None:
                total = reported

            if not requisitions:
                break

            for requisition in requisitions:
                requisition_id = str(requisition.get("Id") or "").strip()
                collected.append(
                    build_job(
                        company_name=company_name,
                        title=str(requisition.get("Title") or ""),
                        job_url=(
                            f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}"
                            f"/job/{requisition_id}"
                            if requisition_id
                            else ""
                        ),
                        location=_location_of(requisition),
                        career_page_url=board_url,
                        platform=PLATFORM,
                    )
                )

            offset += len(requisitions)
            logger.debug("Oracle: page {} gave {} requisition(s)", page, len(requisitions))

            if total is not None and offset >= total:
                break
            if len(requisitions) < _PAGE_SIZE:
                break
        else:
            raise AdapterHttpError(
                f"Oracle paging did not terminate for {api_url} after {_MAX_PAGES} pages"
            )
    finally:
        if owned:
            http.close()

    jobs = dedupe(collected)
    logger.success("Oracle: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
