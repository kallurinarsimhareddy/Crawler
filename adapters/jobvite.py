"""Extract job postings from Jobvite job boards.

Jobvite's REST API is credentialed, but every hosted board is server-rendered
and lists all of a company's open jobs on one page::

    https://jobs.jobvite.com/<company>

Rows carry the posting link (``/<company>/job/<id>``) and a location beside the
title, both under stable ``jv-``-prefixed class names.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from adapters.generic import extract_jobs
from models.job import Job
from utils.html import absolute_url, clean_text, parse_html
from utils.http import AdapterUrlError, build_session, get_text
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Jobvite"

#: Links to an individual posting.
_JOB_HREF: Final[re.Pattern[str]] = re.compile(r"/job/[A-Za-z0-9]", re.IGNORECASE)

#: Class names Jobvite uses for the location line of a listing row.
_LOCATION_CLASS: Final[re.Pattern[str]] = re.compile(
    r"jv-job-list-location|jv-job-detail-location|location", re.IGNORECASE
)

#: Path segments belonging to Jobvite's routing rather than a company.
_RESERVED: Final[frozenset] = frozenset({"careers", "jobs", "search", "job"})


def parse_board_url(career_url: str) -> str:
    """Reduce a Jobvite URL to its company board.

    Args:
        career_url: A Jobvite board or posting URL.

    Returns:
        The board URL, e.g. ``https://jobs.jobvite.com/acme``.

    Raises:
        AdapterUrlError: If the URL names no company.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Jobvite URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Jobvite URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise AdapterUrlError(f"Jobvite URL has no hostname: {career_url!r}")

    for segment in (segment for segment in parts.path.split("/") if segment):
        if segment.lower() in _RESERVED:
            continue
        return f"https://{host}/{segment}"

    raise AdapterUrlError(
        f"No Jobvite company in {career_url!r} "
        "(expected something like https://jobs.jobvite.com/<company>)"
    )


def _extract_rows(markup: str, page_url: str, company_name: str, board_url: str) -> List[Job]:
    """Parse a Jobvite board listing.

    Args:
        markup: Raw HTML of the board.
        page_url: URL the markup came from.
        company_name: Company as named in the input sheet.
        board_url: Board URL recorded on each job.

    Returns:
        The postings on the board, falling back to generic extraction when the
        Jobvite selectors match nothing.
    """
    soup = parse_html(markup)
    collected: List[Optional[Job]] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if not _JOB_HREF.search(href):
            continue

        title = clean_text(anchor)
        if not title:
            continue

        location = ""
        row = anchor.find_parent(["li", "tr", "div"]) or anchor.parent
        if row is not None:
            tag = row.find(attrs={"class": _LOCATION_CLASS})
            if tag is not None:
                text = clean_text(tag)
                if text and text != title:
                    location = text

        collected.append(
            build_job(
                company_name=company_name,
                title=title,
                job_url=absolute_url(page_url, href),
                location=location,
                career_page_url=board_url,
                platform=PLATFORM,
            )
        )

    jobs = dedupe(collected)
    if jobs:
        return jobs

    return extract_jobs(markup, page_url, company_name, PLATFORM, career_page_url=board_url)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Jobvite board.

    Args:
        career_url: Any Jobvite board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names no company.
        AdapterHttpError: If the board cannot be read.
    """
    board_url = parse_board_url(career_url)

    logger.info("Jobvite: board {} for {!r}", board_url, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        markup = get_text(http, board_url)
    finally:
        if owned:
            http.close()

    jobs = _extract_rows(markup, board_url, company_name, board_url)
    logger.success("Jobvite: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
