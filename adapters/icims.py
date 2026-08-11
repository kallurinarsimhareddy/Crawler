"""Extract job postings from iCIMS career portals.

iCIMS portals are served from ``<tenant>.icims.com``, frequently iframed into
the company's own careers page, and they page server-side through a search
results view::

    https://<tenant>.icims.com/jobs/search?ss=1&in_iframe=1&pr=<page>

There is no public JSON API, so the rendered result rows are parsed. Rows carry
the posting link and, in most themes, a location line beside it; anything the
iCIMS-specific selectors miss falls through to the generic job-card extraction.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from adapters._paginated_html import crawl_pages
from adapters.generic import extract_jobs
from models.job import Job
from utils.html import absolute_url, clean_text, parse_html
from utils.http import AdapterHttpError, AdapterUrlError, build_session
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_portal_host"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "iCIMS"

#: Result pages to walk before giving up.
_MAX_PAGES: Final[int] = 60

#: Links to an individual posting: /jobs/<id>/<slug>/job
_JOB_HREF: Final[re.Pattern[str]] = re.compile(r"/jobs/\d+/", re.IGNORECASE)

#: Class names iCIMS themes use for the location line of a result row.
_LOCATION_CLASS: Final[re.Pattern[str]] = re.compile(
    r"iCIMS_JobHeaderTag|JobLocation|job-location|location", re.IGNORECASE
)

#: Markers of the AWS WAF bot challenge iCIMS serves in place of the board.
_WAF_MARKERS: Final[tuple] = ("awsWafCookieDomainList", "Human Verification", "gokuProps")


def parse_portal_host(career_url: str) -> str:
    """Read the iCIMS portal hostname out of a URL.

    Args:
        career_url: An iCIMS portal, search or posting URL.

    Returns:
        The portal hostname.

    Raises:
        AdapterUrlError: If the URL is not an iCIMS portal.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No iCIMS URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable iCIMS URL: {career_url!r} ({exc})") from exc

    if not host.endswith("icims.com"):
        raise AdapterUrlError(
            f"{career_url!r} is not an iCIMS portal URL "
            "(expected something like https://careers-acme.icims.com/jobs/search)"
        )

    return host


def _extract_rows(markup: str, page_url: str, company_name: str, board_url: str) -> List[Job]:
    """Parse one iCIMS search results page.

    Args:
        markup: Raw HTML of the results page.
        page_url: URL the markup came from.
        company_name: Company as named in the input sheet.
        board_url: Portal URL recorded on each job.

    Returns:
        The postings on this page, deduplicated. Falls back to generic
        extraction when the iCIMS selectors match nothing, so an unfamiliar
        theme still yields results.
    """
    if any(marker in markup for marker in _WAF_MARKERS):
        raise AdapterHttpError(
            f"{page_url} served an AWS WAF bot challenge instead of the job board. iCIMS fronts "
            "its portals with a human-verification interstitial that cannot be satisfied over "
            "plain HTTP; reading this tenant needs the browser-driven path"
        )

    soup = parse_html(markup)
    collected: List[Optional[Job]] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if not _JOB_HREF.search(href):
            continue

        title = clean_text(anchor)
        if not title:
            continue

        # The location sits in a tagged element within the same result row.
        location = ""
        row = anchor.find_parent(attrs={"class": re.compile(r"row|job", re.IGNORECASE)}) or anchor.parent
        if row is not None:
            tag = row.find(attrs={"class": _LOCATION_CLASS})
            if tag is not None:
                location = clean_text(tag)

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
    """Fetch every posting on an iCIMS portal.

    Args:
        career_url: Any iCIMS portal, search or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found across the portal's result pages, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` is not an iCIMS URL.
        AdapterHttpError: If the portal's first page cannot be read.
    """
    host = parse_portal_host(career_url)
    board_url = f"https://{host}/jobs/search?ss=1"

    logger.info("iCIMS: portal {} for {!r}", host, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        jobs = crawl_pages(
            http,
            lambda page: f"https://{host}/jobs/search?ss=1&in_iframe=1&pr={page}",
            company_name,
            PLATFORM,
            board_url,
            max_pages=_MAX_PAGES,
            extract=lambda markup, url: _extract_rows(markup, url, company_name, board_url),
            # iCIMS answers the challenge with 403/405; the body is what
            # identifies it, so it must reach the extractor to be reported.
            allow_statuses=(403, 405),
        )
    finally:
        if owned:
            http.close()

    logger.success("iCIMS: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
