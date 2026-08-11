"""Extract job postings from SAP SuccessFactors career sites.

SuccessFactors ships two very different candidate front ends, and only one of
them can be crawled without a browser:

* **Recruiting Marketing / Career Site Builder** — the modern product, served
  from a customer domain such as ``jobs.acme.com``. Its search results are
  server-rendered and paged by row offset::

      https://jobs.acme.com/search/?q=&startrow=<n>

  Rows carry ``a.jobTitle-link`` and ``span.jobLocation``. This is what the
  adapter reads.

* **The legacy career portal** — ``career?company=<id>`` on a
  ``successfactors.com`` / ``successfactors.eu`` host. Its result list is built
  client-side from a stateful session, with no unauthenticated data endpoint, so
  it is rejected with an explanatory error rather than returning an empty board
  that would look like a company with no openings.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from loguru import logger

from adapters._paginated_html import crawl_pages
from adapters.generic import extract_jobs
from models.job import Job
from utils.html import absolute_url, clean_text, parse_html
from utils.http import AdapterUrlError, build_session
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_search_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "SAP SuccessFactors"

#: Result rows per page in Career Site Builder.
_PAGE_SIZE: Final[int] = 25

#: Result pages to walk before giving up.
_MAX_PAGES: Final[int] = 60

#: Hosts running the legacy portal this adapter cannot read.
_LEGACY_HOSTS: Final[tuple] = ("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu")

#: Class names Career Site Builder uses for the title link and location cell.
_TITLE_CLASS: Final[re.Pattern[str]] = re.compile(r"jobTitle-link|jobTitle", re.IGNORECASE)
_LOCATION_CLASS: Final[re.Pattern[str]] = re.compile(r"jobLocation|jobFacility", re.IGNORECASE)


def parse_search_url(career_url: str) -> str:
    """Reduce a SuccessFactors URL to its Career Site Builder search page.

    Args:
        career_url: A SuccessFactors career site or posting URL.

    Returns:
        The search URL the listing pages hang off.

    Raises:
        AdapterUrlError: If the URL has no hostname, or names the legacy portal
            that cannot be read without a browser.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No SuccessFactors URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable SuccessFactors URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise AdapterUrlError(f"SuccessFactors URL has no hostname: {career_url!r}")

    if any(host == legacy or host.endswith(f".{legacy}") for legacy in _LEGACY_HOSTS):
        raise AdapterUrlError(
            f"{career_url!r} is a legacy SuccessFactors career portal. Its job list is built "
            "client-side from a stateful session with no unauthenticated data endpoint, so it "
            "cannot be crawled over plain HTTP; it needs a browser-driven adapter"
        )

    return urlunsplit((parts.scheme or "https", parts.netloc, "/search/", "", ""))


def _extract_rows(markup: str, page_url: str, company_name: str, board_url: str) -> List[Job]:
    """Parse one Career Site Builder results page.

    Args:
        markup: Raw HTML of the results page.
        page_url: URL the markup came from.
        company_name: Company as named in the input sheet.
        board_url: Search URL recorded on each job.

    Returns:
        The postings on this page, falling back to generic extraction when the
        Career Site Builder selectors match nothing.
    """
    soup = parse_html(markup)
    collected: List[Optional[Job]] = []

    for anchor in soup.find_all("a", attrs={"class": _TITLE_CLASS}, href=True):
        title = clean_text(anchor)
        if not title:
            continue

        location = ""
        row = anchor.find_parent(["tr", "li", "div"]) or anchor.parent
        if row is not None:
            tag = row.find(attrs={"class": _LOCATION_CLASS})
            if tag is not None:
                location = clean_text(tag)

        collected.append(
            build_job(
                company_name=company_name,
                title=title,
                job_url=absolute_url(page_url, anchor.get("href")),
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
    """Fetch every posting on a SuccessFactors Career Site Builder site.

    Args:
        career_url: A SuccessFactors career site URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found across the search pages, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names the legacy portal or has no
            hostname.
        AdapterHttpError: If the first search page cannot be read.
    """
    board_url = parse_search_url(career_url)

    logger.info("SuccessFactors: site {} for {!r}", board_url, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        jobs = crawl_pages(
            http,
            lambda page: f"{board_url}?q=&startrow={page * _PAGE_SIZE}",
            company_name,
            PLATFORM,
            board_url,
            max_pages=_MAX_PAGES,
            extract=lambda markup, url: _extract_rows(markup, url, company_name, board_url),
        )
    finally:
        if owned:
            http.close()

    logger.success("SuccessFactors: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
