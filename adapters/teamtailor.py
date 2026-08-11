"""Extract job postings from Teamtailor career sites.

Teamtailor's public API requires a tenant API token, so the crawler reads the
career site instead. Those pages are server-rendered and paged with a plain
query parameter::

    https://<company>.teamtailor.com/jobs?page=<n>

Posting links are ``/jobs/<id>-<slug>``; the department and location sit beside
the title in the same card.
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

__all__ = ["PLATFORM", "fetch_jobs", "parse_site_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Teamtailor"

#: Listing pages to walk before giving up.
_MAX_PAGES: Final[int] = 40

#: Links to an individual posting: /jobs/<id>-<slug>
_JOB_HREF: Final[re.Pattern[str]] = re.compile(r"/jobs/\d+", re.IGNORECASE)

#: Class names Teamtailor themes use for the location line of a card.
_LOCATION_CLASS: Final[re.Pattern[str]] = re.compile(r"location|department|meta", re.IGNORECASE)


def parse_site_url(career_url: str) -> str:
    """Reduce a Teamtailor URL to the site's jobs listing.

    Args:
        career_url: A Teamtailor career site or posting URL.

    Returns:
        The listing URL, e.g. ``https://acme.teamtailor.com/jobs``.

    Raises:
        AdapterUrlError: If the URL has no hostname.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Teamtailor URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Teamtailor URL: {career_url!r} ({exc})") from exc

    if not host:
        raise AdapterUrlError(f"Teamtailor URL has no hostname: {career_url!r}")

    return f"https://{host}/jobs"


def _extract_cards(markup: str, page_url: str, company_name: str, board_url: str) -> List[Job]:
    """Parse one Teamtailor listing page.

    Args:
        markup: Raw HTML of the listing.
        page_url: URL the markup came from.
        company_name: Company as named in the input sheet.
        board_url: Listing URL recorded on each job.

    Returns:
        The postings on this page, falling back to generic extraction when the
        Teamtailor selectors match nothing.
    """
    soup = parse_html(markup)
    collected: List[Optional[Job]] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if not _JOB_HREF.search(href):
            continue

        title = clean_text(anchor.find(["h1", "h2", "h3", "h4", "span"]) or anchor)
        if not title:
            continue

        location = ""
        card = anchor.find_parent(["li", "article", "div"]) or anchor.parent
        if card is not None:
            tag = card.find(attrs={"class": _LOCATION_CLASS})
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
    """Fetch every posting on a Teamtailor career site.

    Args:
        career_url: Any Teamtailor site or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found across the listing's pages, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` has no hostname.
        AdapterHttpError: If the first listing page cannot be read.
    """
    board_url = parse_site_url(career_url)

    logger.info("Teamtailor: site {} for {!r}", board_url, company_name)

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
            extract=lambda markup, url: _extract_cards(markup, url, company_name, board_url),
        )
    finally:
        if owned:
            http.close()

    logger.success("Teamtailor: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
