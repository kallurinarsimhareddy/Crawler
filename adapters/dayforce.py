"""Extract job postings from Ceridian Dayforce candidate portals.

Dayforce runs two candidate front ends, and only one is readable over plain
HTTP:

* **The classic portal**, served per-tenant and server-rendered::

      https://<tenant>.dayforcehcm.com/CandidatePortal/en-US/<site>?page=<n>

  Posting links are ``/Posting/View/<id>`` and each result row carries a
  location, so the rows are read directly. This is what the adapter crawls.

* **The unified portal** at ``jobs.dayforcehcm.com/<namespace>/CANDIDATEPORTAL``
  — a Next.js application that server-renders only its chrome. The job list is
  fetched after hydration, the endpoint is assembled at runtime rather than
  appearing in the static bundles, and the per-tenant pods named in the page
  config (``us252.dayforcehcm.com`` and similar) do not resolve publicly. There
  is therefore no reachable data endpoint to call, so those URLs raise an
  explanatory error rather than reporting a company as having no openings.
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
from utils.http import AdapterUrlError, build_session
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_portal_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Dayforce"

#: Result pages to walk before giving up.
_MAX_PAGES: Final[int] = 40

#: Links to an individual posting.
_JOB_HREF: Final[re.Pattern[str]] = re.compile(r"/Posting/View/\d+", re.IGNORECASE)

#: Class names Dayforce themes use for the location line of a result row.
_LOCATION_CLASS: Final[re.Pattern[str]] = re.compile(r"location|address|city", re.IGNORECASE)


def parse_portal_url(career_url: str) -> str:
    """Reduce a Dayforce URL to its classic candidate-portal listing.

    Args:
        career_url: A Dayforce portal or posting URL.

    Returns:
        The portal listing URL.

    Raises:
        AdapterUrlError: If the URL names no candidate portal, or names the
            unified ``jobs.dayforcehcm.com`` portal, which serves no data
            endpoint this adapter can reach.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Dayforce URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Dayforce URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise AdapterUrlError(f"Dayforce URL has no hostname: {career_url!r}")

    if host == "jobs.dayforcehcm.com" or host.endswith(".jobs.dayforcehcm.com"):
        raise AdapterUrlError(
            f"{career_url!r} is a unified Dayforce portal. Its job list is rendered client-side "
            "after hydration, the search endpoint is assembled at runtime rather than published "
            "in the page or its bundles, and the tenant pod named in the site config does not "
            "resolve publicly, so there is no HTTP endpoint to read; it needs a browser-driven "
            "adapter"
        )

    segments = [segment for segment in parts.path.split("/") if segment]

    # Keep everything up to the site name; drop /Posting/... detail segments.
    for index, segment in enumerate(segments):
        if segment.lower() in {"posting", "jobs", "job"}:
            segments = segments[:index]
            break

    if not segments or "candidateportal" not in {segment.lower() for segment in segments}:
        raise AdapterUrlError(
            f"No Dayforce candidate portal in {career_url!r} "
            "(expected .../CandidatePortal/en-US/<site>)"
        )

    return f"https://{host}/" + "/".join(segments)


def _extract_rows(markup: str, page_url: str, company_name: str, board_url: str) -> List[Job]:
    """Parse one Dayforce results page.

    Args:
        markup: Raw HTML of the results page.
        page_url: URL the markup came from.
        company_name: Company as named in the input sheet.
        board_url: Portal URL recorded on each job.

    Returns:
        The postings on this page, falling back to generic extraction when the
        Dayforce selectors match nothing.
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
        row = anchor.find_parent(["li", "article", "div"]) or anchor.parent
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
    """Fetch every posting on a Dayforce candidate portal.

    Args:
        career_url: Any Dayforce portal or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found across the portal's pages, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names no portal.
        AdapterHttpError: If the portal's first page cannot be read.
    """
    board_url = parse_portal_url(career_url)

    logger.info("Dayforce: portal {} for {!r}", board_url, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        jobs = crawl_pages(
            http,
            lambda page: board_url if page <= 1 else f"{board_url}?page={page}",
            company_name,
            PLATFORM,
            board_url,
            max_pages=_MAX_PAGES,
            first_page=1,
            extract=lambda markup, url: _extract_rows(markup, url, company_name, board_url),
        )
    finally:
        if owned:
            http.close()

    logger.success("Dayforce: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
