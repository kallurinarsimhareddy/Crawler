"""Extract job postings from Eightfold AI career sites.

Eightfold's board is a single-page application, but it is driven by an
unauthenticated JSON API that returns the same postings the page renders::

    GET https://<host>/api/apply/v2/jobs?domain=<domain>&start=0&num=100

The ``domain`` parameter is the tenant key — usually the company's own web
domain rather than the board host, so a board at ``acme.eightfold.ai`` is keyed
``acme.com``. It is read from the board URL when present, and otherwise from
the board page itself, which embeds it in its bootstrap configuration. Guessing
it from the host is the last resort.

Eightfold caps ``num`` server-side, so the board is walked with ``start``
until a page comes back short or the reported total is reached.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.discovery import text_of
from utils.http import AdapterError, AdapterHttpError, AdapterUrlError, build_session, get_json, get_text
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_site"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Eightfold"

#: Postings requested per call. Eightfold clamps this server-side.
_PAGE_SIZE: Final[int] = 100

#: Hard stop on pages, so a board that never shortens cannot hang a run.
_MAX_PAGES: Final[int] = 60

#: The tenant key as the board page embeds it in its bootstrap config.
_DOMAIN_IN_PAGE: Final[re.Pattern[str]] = re.compile(
    r"""["']domain["']\s*:\s*["']([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})["']""", re.IGNORECASE
)

#: Host labels that belong to Eightfold rather than to the tenant.
_NON_TENANT: Final[frozenset] = frozenset({"www", "careers", "jobs", "app", "api"})


def parse_site(career_url: str) -> Tuple[str, str]:
    """Read the board origin and tenant domain out of an Eightfold URL.

    Args:
        career_url: An Eightfold board or posting URL.

    Returns:
        ``(origin, domain)`` — the board's scheme and host, and the tenant key
        for the API. ``domain`` is ``""`` when the URL does not carry one, in
        which case :func:`fetch_jobs` reads it from the board page.

    Raises:
        AdapterUrlError: If the URL has no usable host.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Eightfold URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Eightfold URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise AdapterUrlError(f"No host in Eightfold URL {career_url!r}")

    domain = (parse_qs(parts.query).get("domain") or [""])[0].strip().lower()

    return f"{parts.scheme or 'https'}://{host}", domain


def _guess_domain(origin: str) -> str:
    """Guess the tenant key from the board host.

    Args:
        origin: The board's scheme and host.

    Returns:
        A plausible tenant key. Eightfold tenants at ``<name>.eightfold.ai``
        are almost always keyed ``<name>.com``; a board on the company's own
        domain is keyed by that domain.
    """
    host = (urlsplit(origin).hostname or "").lower()

    if host.endswith("eightfold.ai"):
        label = host.split(".")[0]
        return f"{label}.com" if label not in _NON_TENANT else ""

    labels = host.split(".")
    while labels and labels[0] in _NON_TENANT:
        labels = labels[1:]
    return ".".join(labels) if len(labels) >= 2 else ""


def _resolve_domain(session: requests.Session, origin: str, career_url: str) -> str:
    """Find the tenant key, reading the board page if the URL did not carry it.

    Args:
        session: Session to use.
        origin: The board's scheme and host.
        career_url: The original URL, fetched to read its bootstrap config.

    Returns:
        The tenant key, or ``""`` if none could be found.
    """
    try:
        markup = get_text(session, career_url)
    except AdapterError as exc:
        logger.debug("Eightfold: could not read {} for its domain ({})", career_url, exc)
        return _guess_domain(origin)

    match = _DOMAIN_IN_PAGE.search(markup)
    if match is not None:
        return match.group(1).lower()

    return _guess_domain(origin)


def _location_of(posting: Dict[str, Any]) -> str:
    """Read the location from an Eightfold posting.

    Args:
        posting: One entry of the ``positions`` list.

    Returns:
        The location text, or ``""``. Multi-location postings are joined,
        which :func:`utils.location.derive_country` then refuses to reduce to a
        single country — correctly, since there is more than one.
    """
    for key in ("location", "locations", "work_location", "displayLocation"):
        found = text_of(posting.get(key))
        if found:
            return found
    return ""


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an Eightfold career site.

    Args:
        career_url: Any Eightfold board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated. Empty if the board advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` has no usable host, or the tenant
            key cannot be determined.
        AdapterHttpError: If the API cannot be read.
    """
    origin, domain = parse_site(career_url)
    board_url = f"{origin}/careers"

    owned = session is None
    http = session if session is not None else build_session()

    collected: List[Optional[Job]] = []

    try:
        if not domain:
            domain = _resolve_domain(http, origin, career_url)
        if not domain:
            raise AdapterUrlError(
                f"Could not determine the Eightfold tenant key for {career_url!r}; "
                "the board URL carries no ?domain= and the page embeds none"
            )

        api_url = f"{origin}/api/apply/v2/jobs"
        logger.info("Eightfold: tenant {!r} at {} for {!r}", domain, origin, company_name)

        total: Optional[int] = None

        for page in range(_MAX_PAGES):
            start = page * _PAGE_SIZE
            payload = get_json(
                http,
                api_url,
                params={
                    "domain": domain,
                    "start": start,
                    "num": _PAGE_SIZE,
                    "sort_by": "relevance",
                    "triggerGoButton": "false",
                },
            )

            if not isinstance(payload, dict):
                raise AdapterHttpError(
                    f"Eightfold returned {type(payload).__name__} for {api_url}"
                )

            positions = payload.get("positions")
            if not isinstance(positions, list):
                if page == 0:
                    raise AdapterHttpError(f"Eightfold returned no positions list for {api_url}")
                break

            if total is None:
                count = payload.get("count")
                total = int(count) if isinstance(count, (int, float)) else None
                if total:
                    logger.debug("Eightfold: {!r} reports {} posting(s)", domain, total)

            for posting in positions:
                if not isinstance(posting, dict):
                    continue
                identifier = text_of(posting.get("id") or posting.get("pid"))
                collected.append(
                    build_job(
                        company_name=company_name,
                        title=text_of(posting.get("name") or posting.get("title")),
                        job_url=text_of(posting.get("canonicalPositionUrl"))
                        or (f"{board_url}?pid={identifier}" if identifier else ""),
                        location=_location_of(posting),
                        career_page_url=board_url,
                        platform=PLATFORM,
                    )
                )

            if len(positions) < _PAGE_SIZE:
                break
            if total is not None and start + len(positions) >= total:
                break
        else:
            logger.warning("Eightfold: {!r} never stopped paging, stopped at {}", domain, _MAX_PAGES)
    finally:
        if owned:
            http.close()

    jobs = dedupe(collected)
    logger.success("Eightfold: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
