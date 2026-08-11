"""Shared reader for the ``/ta/`` recruitment app behind UKG Ready and Asure.

Both products run the same application, differing only in the host that serves
it, and both publish an unauthenticated REST endpoint that the board's own
JavaScript calls::

    https://<host>/ta/<company>.careers                     # the board
    https://<host>/ta/rest/ui/recruitment/companies/%7C<company>/job-requisitions

The rendered board is useless to a scraper — every anchor on it is ``#``, and
the rows are painted by React from that endpoint — so the endpoint is the only
honest way to read one. Version 1 and the first version 2 pass both returned
zero jobs for every company on these platforms for exactly that reason.

The ``%7C`` is a URL-encoded pipe the API expects in front of the company id.

This is a private helper for :mod:`adapters`; it registers no platform of its
own and the engine never looks it up.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.discovery import text_of
from utils.http import AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["fetch_ta_board", "parse_ta_url"]

#: Requisitions requested per call.
_PAGE_SIZE: Final[int] = 200

#: Hard stop on pages, so a board that never shortens cannot hang a run.
_MAX_PAGES: Final[int] = 40

#: ``/ta/6095384.careers`` — the digits are the company id the API is keyed by.
_COMPANY_ID: Final[re.Pattern[str]] = re.compile(r"/ta/(\d+)\b", re.IGNORECASE)


def parse_ta_url(career_url: str, platform: str, hosts: Sequence[str]) -> Tuple[str, str]:
    """Read the board host and company id out of a ``/ta/`` careers URL.

    Args:
        career_url: The URL from the input sheet.
        platform: Label for the ``Platform`` column, used in error messages.
        hosts: Registrable domains this vendor serves boards from.

    Returns:
        ``(origin, company_id)``.

    Raises:
        AdapterUrlError: If the URL is not this vendor's, or names no company.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError(f"No {platform} URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable {platform} URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise AdapterUrlError(f"No host in {platform} URL {career_url!r}")

    if hosts and not any(host == domain or host.endswith(f".{domain}") for domain in hosts):
        raise AdapterUrlError(
            f"{career_url!r} is not a {platform} board (expected a host under {', '.join(hosts)})"
        )

    match = _COMPANY_ID.search(parts.path)
    if match is None:
        raise AdapterUrlError(
            f"No {platform} company id in {career_url!r} "
            "(expected something like https://<host>/ta/<id>.careers)"
        )

    return f"{parts.scheme or 'https'}://{host}", match.group(1)


def _location_of(requisition: Dict[str, Any]) -> str:
    """Assemble the location from a requisition.

    Args:
        requisition: One entry of ``job_requisitions``.

    Returns:
        ``"City, State, Country"`` with missing parts omitted. The country is
        left in the text rather than reported separately, so
        :func:`utils.location.derive_country` can resolve it from whichever
        part is actually present.
    """
    node = requisition.get("location")
    if not isinstance(node, dict):
        return text_of(node)

    parts = [
        text_of(node.get("city")),
        text_of(node.get("state")),
        text_of(node.get("country")),
    ]
    return ", ".join(part for part in parts if part)


def fetch_ta_board(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session],
    platform: str,
    hosts: Sequence[str],
) -> List[Job]:
    """Read every open requisition from a ``/ta/`` recruitment board.

    Args:
        career_url: The URL from the input sheet.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when ``None``.
        platform: Label for the ``Platform`` column.
        hosts: Registrable domains this vendor serves boards from.

    Returns:
        Every requisition, deduplicated. Empty if the board advertises nothing.

    Raises:
        AdapterUrlError: If ``career_url`` is not usable for this vendor.
        AdapterHttpError: If the endpoint cannot be read.
    """
    origin, company_id = parse_ta_url(career_url, platform, hosts)
    board_url = f"{origin}/ta/{company_id}.careers"
    api_url = f"{origin}/ta/rest/ui/recruitment/companies/%7C{company_id}/job-requisitions"

    logger.info("{}: company {!r} at {} for {!r}", platform, company_id, origin, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    collected: List[Optional[Job]] = []

    try:
        # The API counts from one, not zero.
        for page in range(_MAX_PAGES):
            payload = get_json(
                http,
                api_url,
                params={
                    "offset": page * _PAGE_SIZE + 1,
                    "size": _PAGE_SIZE,
                    "sort": "desc",
                    "ein_id": "",
                    "lang": "en-US",
                },
                headers={"Accept": "application/json"},
            )

            requisitions = payload.get("job_requisitions") if isinstance(payload, dict) else None
            if not isinstance(requisitions, list) or not requisitions:
                break

            for requisition in requisitions:
                if not isinstance(requisition, dict):
                    continue
                identifier = text_of(requisition.get("id"))
                collected.append(
                    build_job(
                        company_name=company_name,
                        title=text_of(requisition.get("job_title")),
                        job_url=f"{board_url}?ApplyToJob={identifier}" if identifier else "",
                        location=_location_of(requisition),
                        career_page_url=board_url,
                        platform=platform,
                    )
                )

            if len(requisitions) < _PAGE_SIZE:
                break
        else:
            logger.warning("{}: {!r} never stopped paging", platform, company_id)
    finally:
        if owned:
            http.close()

    jobs = dedupe(collected)
    logger.success("{}: {} job(s) for {!r}", platform, len(jobs), company_name)
    return jobs
