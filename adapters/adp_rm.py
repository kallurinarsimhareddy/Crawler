"""Extract job postings from ADP Recruiting Management boards.

ADP sells two different recruiting products and they share a domain, which is
why they need two adapters. :mod:`adapters.adp` handles the WorkforceNow board
at ``workforcenow.adp.com/mascsr/...``, which exposes a JSON endpoint. This
module handles the other one::

    https://myjobs.adp.com/<slug>/cx/job-listing

Recruiting Management renders entirely client-side and publishes no anonymous
API, so version 1 refused it outright and reported "no public endpoint" for
every company on it. What does work is to let the page fetch its own listings
and keep what it fetched: the board is rendered in headless Chromium and the
JSON responses it made are mined.

The one thing that payload does not carry is a link. Each posting names itself
by ``reqId`` only, and the board turns that into a URL client-side — so this
adapter rebuilds it the same way. Without that step every posting is dropped
for having no URL, which is precisely what made this platform report zero jobs
across twenty-two companies on the first version 2 run.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from adapters._paginated_html import normalise_board_url
from config.settings import SETTINGS
from models.job import Job
from utils.discovery import jobs_from_payload, text_of
from utils.jobs import dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "ADP Recruiting Management"

#: The host this product is served from. Deliberately narrower than
#: ``adp.com``, so a WorkforceNow board never reaches this adapter.
_HOSTS: Final[tuple] = ("myjobs.adp.com",)

#: ``/acme/cx/job-listing`` — the first segment is the client's board slug.
_SLUG: Final[re.Pattern[str]] = re.compile(r"^/([^/]+)/", re.IGNORECASE)


def parse_board_url(career_url: str) -> str:
    """Validate an ADP Recruiting Management board URL.

    Args:
        career_url: The URL from the input sheet.

    Returns:
        The board URL, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is not a Recruiting Management board.
    """
    return normalise_board_url(career_url, PLATFORM, _HOSTS)


def _board_slug(board_url: str) -> str:
    """Read the client's board slug out of the URL.

    Args:
        board_url: A normalised board URL.

    Returns:
        The slug, or ``""`` if the path carries none.
    """
    match = _SLUG.match(urlsplit(board_url).path or "")
    return match.group(1) if match else ""


def _posting_url(posting: Dict[str, Any], page_url: str, origin: str, slug: str) -> str:
    """Rebuild a posting's own URL from its requisition id.

    The captured payload names each posting by id only, so the link the board
    would have rendered is reconstructed here.

    Args:
        posting: One key-normalised posting object from the captured payload.
        page_url: URL the payload came from. Unused; present because
            :func:`utils.discovery.jobs_from_payload` passes it.
        origin: The board's scheme and host.
        slug: The client's board slug.

    Returns:
        An absolute posting URL, or ``""`` when the object names no id.
    """
    identifier = text_of(
        posting.get("reqid") or posting.get("clientrequisitionid") or posting.get("id")
    )
    if not identifier or not slug:
        return ""

    return f"{origin}/{slug}/cx/job-details?reqId={identifier}"


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an ADP Recruiting Management board.

    Args:
        career_url: Any ``myjobs.adp.com`` URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. Unused — this board cannot be read over
            plain HTTP — but accepted so the adapter contract holds.

    Returns:
        Every posting found, deduplicated. Empty when the board advertises
        nothing, or when the run forbids the browser: this board cannot be read
        without one.

    Raises:
        AdapterUrlError: If ``career_url`` is not a Recruiting Management board.
    """
    board_url = parse_board_url(career_url)

    if not SETTINGS.browser_fallback:
        logger.info(
            "ADP Recruiting Management: {!r} needs a browser and the run forbids one",
            company_name,
        )
        return []

    parts = urlsplit(board_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    slug = _board_slug(board_url)

    logger.info("ADP Recruiting Management: rendering {} for {!r}", board_url, company_name)

    # Imported here so a browserless run never pulls in the module.
    from utils.browser import render

    page = render(board_url)
    if page is None or not page.ok:
        logger.debug("ADP Recruiting Management: no usable render for {!r}", company_name)
        return []

    # Accumulate across every captured response rather than stopping at the
    # first that yields postings. The board fetches a page of ten at a time as
    # the visitor scrolls, so each page is a separate response and taking only
    # the first would cap every company at ten.
    collected: List[Optional[Job]] = []
    for payload in page.payloads:
        collected.extend(
            jobs_from_payload(
                payload,
                page.url,
                company_name,
                PLATFORM,
                career_page_url=board_url,
                url_builder=lambda posting, url: _posting_url(posting, url, origin, slug),
            )
        )

    jobs = dedupe(collected)
    logger.success("ADP Recruiting Management: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
