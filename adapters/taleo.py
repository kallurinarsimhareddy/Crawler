"""Extract job postings from Oracle Taleo career sections.

Taleo renders its job search with JavaScript, but the page is driven by a REST
endpoint on the same host that answers without credentials::

    POST https://<tenant>.taleo.net/careersection/rest/jobboard/searchjobs?lang=en
    {"fieldData": {...}, "pageNo": 1}

The response returns rows as a ``column`` array whose order follows the
tenant's configured result columns — title first in every deployment seen —
plus the ``contestNo`` that addresses the posting's detail page.

Tenants that have disabled the REST board fall back to parsing the rendered
search page, which yields results only where the section is server-rendered.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from loguru import logger

from adapters.generic import extract_jobs
from models.job import Job
from utils.http import AdapterError, AdapterHttpError, AdapterUrlError, build_session, get_text, post_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_career_section"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Taleo"

#: Result pages to walk before giving up.
_MAX_PAGES: Final[int] = 100

#: Looks like a location rather than a date or a requisition number.
_LOCATION_LIKE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]{2,},|[A-Za-z]{4,}")

#: Values that are clearly not a location.
_DATE_LIKE: Final[re.Pattern[str]] = re.compile(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}$")


def parse_career_section(career_url: str) -> Tuple[str, str]:
    """Read the Taleo host and career section out of a URL.

    Args:
        career_url: A Taleo career section or posting URL.

    Returns:
        ``(host, section)``. ``section`` is ``""`` when the URL names none,
        which still allows the REST board to be queried.

    Raises:
        AdapterUrlError: If the URL is not a Taleo URL.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Taleo URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Taleo URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise AdapterUrlError(f"Taleo URL has no hostname: {career_url!r}")

    segments = [segment for segment in parts.path.split("/") if segment]
    for index, segment in enumerate(segments):
        if segment.lower() == "careersection" and index + 1 < len(segments):
            candidate = segments[index + 1]
            # ".../careersection/jobsearch.ftl" names no section.
            return host, "" if candidate.endswith(".ftl") else candidate

    return host, ""


def _row_values(row: Dict[str, Any]) -> Tuple[str, str]:
    """Read the title and location out of one Taleo result row.

    Args:
        row: One entry of ``requisitionList``.

    Returns:
        ``(title, location)``; either may be ``""``.
    """
    columns = row.get("column")
    values = [str(value).strip() for value in columns if value] if isinstance(columns, list) else []

    title = str(row.get("jobTitle") or (values[0] if values else "")).strip()

    for value in values[1:]:
        if value == title or _DATE_LIKE.match(value):
            continue
        if _LOCATION_LIKE.search(value):
            return title, value

    return title, ""


def _fetch_via_rest(
    session: requests.Session, host: str, section: str, company_name: str, board_url: str
) -> List[Job]:
    """Walk the REST job board.

    Args:
        session: Session to use.
        host: Taleo hostname.
        section: Career section name, used to build posting URLs.
        company_name: Company as named in the input sheet.
        board_url: Board URL recorded on each job.

    Returns:
        Every posting the REST board reports, deduplicated.

    Raises:
        AdapterHttpError: If the endpoint cannot be read.
    """
    api_url = f"https://{host}/careersection/rest/jobboard/searchjobs"
    collected: List[Optional[Job]] = []
    detail_base = f"https://{host}/careersection/{section}/jobdetail.ftl" if section else ""

    for page in range(1, _MAX_PAGES + 1):
        payload = post_json(
            session,
            api_url,
            {
                "multilineEnabled": False,
                "sortingSelection": {
                    "sortBySelectionParam": "3",
                    "ascendingSortingOrder": "false",
                },
                "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
                "filterSelectionParam": {"searchFilterSelections": []},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
                "pageNo": page,
            },
            params={"lang": "en"},
            headers={"Referer": board_url},
        )

        if not isinstance(payload, dict):
            raise AdapterHttpError(f"Taleo returned {type(payload).__name__} for {api_url}")

        rows = payload.get("requisitionList")
        if not isinstance(rows, list) or not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue

            title, location = _row_values(row)
            contest = str(row.get("contestNo") or row.get("jobId") or "").strip()
            job_url = f"{detail_base}?job={contest}" if detail_base and contest else ""

            collected.append(
                build_job(
                    company_name=company_name,
                    title=title,
                    job_url=job_url,
                    location=location,
                    career_page_url=board_url,
                    platform=PLATFORM,
                )
            )

        logger.debug("Taleo: page {} gave {} row(s)", page, len(rows))

        paging = payload.get("pagingData")
        total = paging.get("totalCount") if isinstance(paging, dict) else None
        if isinstance(total, int) and len(collected) >= total:
            break

    return dedupe(collected)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting in a Taleo career section.

    Args:
        career_url: Any Taleo career section or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found, deduplicated. Empty when the tenant serves neither
        the REST board nor a server-rendered search page.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Taleo URL.
        AdapterHttpError: If neither route can be read.
    """
    host, section = parse_career_section(career_url)
    board_url = (
        f"https://{host}/careersection/{section}/jobsearch.ftl"
        if section
        else f"https://{host}/careersection/jobsearch.ftl"
    )

    logger.info("Taleo: host {} section {!r} for {!r}", host, section or "<none>", company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        try:
            jobs = _fetch_via_rest(http, host, section, company_name, board_url)
        except AdapterError as exc:
            logger.debug("Taleo: REST board unusable ({}), reading the search page", exc)
            jobs = []

        if not jobs:
            markup = get_text(http, str(career_url).strip() or board_url)
            jobs = extract_jobs(
                markup, board_url, company_name, PLATFORM, career_page_url=board_url
            )
    finally:
        if owned:
            http.close()

    logger.success("Taleo: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
