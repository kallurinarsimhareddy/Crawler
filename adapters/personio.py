"""Extract job postings from Personio career pages.

Personio publishes every tenant's board as an unauthenticated XML feed beside
the board itself, containing the whole board in one document::

    GET https://<tenant>.jobs.personio.de/xml

The feed is the one Personio itself documents for job aggregators, so it is
both stable and complete — considerably more so than the rendered board, which
is a single-page application.

Tenants exist on both ``personio.de`` and ``personio.com``; the feed lives on
whichever host the board does, so the URL's own domain is preserved rather than
assumed.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional, Tuple
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests
from loguru import logger

from models.job import Job
from utils.html import clean_text
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_text
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_board"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Personio"

#: Host labels that are Personio's own plumbing, never a tenant.
_NON_TENANT: Final[frozenset] = frozenset({"www", "jobs", "api", "app"})

#: Tenant labels are lowercase identifiers.
_TENANT: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]*$", re.IGNORECASE)

#: Feed elements naming where a posting sits, best first.
_LOCATION_ELEMENTS: Final[Tuple[str, ...]] = ("office", "location", "city", "subcompany")


def parse_board(career_url: str) -> str:
    """Read the board URL out of a Personio career page URL.

    Args:
        career_url: Any ``<tenant>.jobs.personio.de`` or ``.com`` URL.

    Returns:
        The board's root URL, e.g. ``https://acme.jobs.personio.de``.

    Raises:
        AdapterUrlError: If the URL names no Personio board.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Personio URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Personio URL: {career_url!r} ({exc})") from exc

    if "personio." not in host:
        raise AdapterUrlError(
            f"{career_url!r} is not a Personio board "
            "(expected https://<tenant>.jobs.personio.de)"
        )

    label = host.split(".")[0]
    if label in _NON_TENANT or not _TENANT.match(label):
        raise AdapterUrlError(f"No Personio tenant in {career_url!r}")

    return f"https://{host}"


def _text(node: ElementTree.Element, name: str) -> str:
    """Read one child element's text.

    Args:
        node: The ``<position>`` element.
        name: Child element name.

    Returns:
        The text, whitespace-collapsed, or ``""``.
    """
    child = node.find(name)
    return clean_text(child.text) if child is not None and child.text else ""


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Personio board.

    Args:
        career_url: Any Personio board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` names no Personio board.
        AdapterHttpError: If the feed cannot be read or is not XML.
    """
    board_url = parse_board(career_url)
    feed_url = f"{board_url}/xml"

    logger.info("Personio: feed {} for {!r}", feed_url, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        body = get_text(http, feed_url, headers={"Accept": "application/xml, text/xml, */*"})
    finally:
        if owned:
            http.close()

    try:
        root = ElementTree.fromstring(body.strip())
    except ElementTree.ParseError as exc:
        raise AdapterHttpError(f"Personio returned a non-XML feed for {feed_url}: {exc}") from exc

    positions = root.findall(".//position")
    if not positions:
        logger.debug("Personio: {} lists no positions", feed_url)

    collected = []
    for position in positions:
        identifier = _text(position, "id")
        location = next(
            (found for element in _LOCATION_ELEMENTS if (found := _text(position, element))),
            "",
        )

        collected.append(
            build_job(
                company_name=company_name,
                title=_text(position, "name"),
                job_url=f"{board_url}/job/{identifier}" if identifier else "",
                location=location,
                career_page_url=board_url,
                platform=PLATFORM,
            )
        )

    jobs = dedupe(collected)
    logger.success("Personio: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
