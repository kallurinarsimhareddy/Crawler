"""Extract job postings from Manatal career pages.

Manatal serves every public career page from one unauthenticated API keyed by
the page slug, paged
by a ``next`` cursor::

    GET https://api.manatal.com/open/v3/career-page/<slug>/jobs/

The slug is either the leftmost label of a ``<slug>.manatal.com`` board or the
first path segment of a ``careers.manatal.com/<slug>`` one.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.discovery import text_of
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["API_ROOT", "PLATFORM", "fetch_jobs", "parse_slug"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Manatal"

#: The public career-page API.
API_ROOT: Final[str] = "https://api.manatal.com/open/v3/career-page"

#: Host labels and path segments that are Manatal's plumbing, never the slug.
_NON_SLUG: Final[frozenset] = frozenset(
    {"www", "api", "app", "careers", "career", "jobs", "job", "open"}
)

#: Slugs are lowercase identifiers.
_SLUG: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)

#: Hard stop on cursor pages, so a looping cursor cannot hang a run.
_MAX_PAGES: Final[int] = 60


def parse_slug(career_url: str) -> str:
    """Read the career-page slug out of a Manatal URL.

    Args:
        career_url: A Manatal career page or posting URL.

    Returns:
        The career-page slug.

    Raises:
        AdapterUrlError: If no slug can be read from the URL.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Manatal URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Manatal URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    label = host.split(".")[0]
    if label not in _NON_SLUG and _SLUG.match(label) and host.endswith("manatal.com"):
        return label

    for segment in (segment for segment in parts.path.split("/") if segment):
        if segment.lower() in _NON_SLUG:
            continue
        if _SLUG.match(segment):
            return segment

    raise AdapterUrlError(
        f"No Manatal career-page slug in {career_url!r} "
        "(expected something like https://<slug>.manatal.com)"
    )


def _job_url(posting: Dict[str, Any], board_url: str) -> str:
    """Work out a posting's own URL.

    Args:
        posting: One entry of the ``results`` list.
        board_url: The career page, for building a URL from an identifier.

    Returns:
        An absolute URL, or ``""`` when the posting can be given none.
    """
    for key in ("url", "job_url", "public_url", "career_page_url"):
        found = text_of(posting.get(key))
        if found.startswith("http"):
            return found

    identifier = text_of(posting.get("hash") or posting.get("uuid") or posting.get("id"))
    return f"{board_url}/job/{identifier}" if identifier else ""


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Manatal career page.

    Args:
        career_url: Any Manatal career page or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the page, deduplicated. Empty if it advertises
        nothing.

    Raises:
        AdapterUrlError: If ``career_url`` names no career page.
        AdapterHttpError: If the API cannot be read.
    """
    slug = parse_slug(career_url)
    board_url = f"https://{slug}.manatal.com"
    api_url = f"{API_ROOT}/{slug}/jobs/"

    logger.info("Manatal: career page {!r} for {!r}", slug, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    collected: List[Optional[Job]] = []
    next_url: Optional[str] = api_url

    try:
        for page in range(_MAX_PAGES):
            payload = get_json(http, next_url)

            if not isinstance(payload, dict):
                if page == 0:
                    raise AdapterHttpError(f"Manatal returned {type(payload).__name__} for {api_url}")
                break

            results = payload.get("results")
            if not isinstance(results, list):
                if page == 0:
                    raise AdapterHttpError(f"Manatal returned no results list for {api_url}")
                break

            for posting in results:
                if not isinstance(posting, dict):
                    continue
                collected.append(
                    build_job(
                        company_name=company_name,
                        title=text_of(posting.get("position_name") or posting.get("title")),
                        job_url=_job_url(posting, board_url),
                        location=text_of(posting.get("location")),
                        country=text_of(posting.get("country") or posting.get("country_code")),
                        career_page_url=board_url,
                        platform=PLATFORM,
                    )
                )

            following = payload.get("next")
            next_url = following if isinstance(following, str) and following else None
            if not next_url:
                break
        else:
            logger.warning("Manatal: {!r} never stopped paging, stopping at {}", slug, _MAX_PAGES)
    finally:
        if owned:
            http.close()

    jobs = dedupe(collected)
    logger.success("Manatal: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
