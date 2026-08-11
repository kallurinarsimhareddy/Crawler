"""Extract job postings from SmartRecruiters job boards.

SmartRecruiters publishes every company's live postings through a public API
that needs no credentials::

    GET https://api.smartrecruiters.com/v1/companies/<company>/postings?offset=0&limit=100

The response reports ``totalFound``, and pages are walked with ``offset`` until
every posting has been seen. The company identifier is the first path segment
of ``jobs.smartrecruiters.com/<company>``.
"""

from __future__ import annotations

from typing import Any, Dict, Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_company_id"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "SmartRecruiters"

#: Postings per request. The API rejects anything above 100.
_PAGE_SIZE: Final[int] = 100

#: Hard stop on pagination, far above any real board.
_MAX_PAGES: Final[int] = 200

#: Path segments belonging to SmartRecruiters' routing rather than a company.
_RESERVED: Final[frozenset] = frozenset({"v1", "companies", "postings", "api", "jobs", "search"})


def parse_company_id(career_url: str) -> str:
    """Read the company identifier out of a SmartRecruiters URL.

    Args:
        career_url: A SmartRecruiters board or posting URL.

    Returns:
        The company identifier.

    Raises:
        AdapterUrlError: If the URL names no company.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No SmartRecruiters URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable SmartRecruiters URL: {career_url!r} ({exc})") from exc

    for segment in (segment for segment in parts.path.split("/") if segment):
        if segment.lower() in _RESERVED:
            continue
        return segment

    # Tenants also front the board at "<company>.smartrecruiters.com".
    label = (parts.hostname or "").lower().split(".")[0]
    if label and label not in {"jobs", "careers", "www", "api"}:
        return label

    raise AdapterUrlError(
        f"No SmartRecruiters company in {career_url!r} "
        "(expected something like https://jobs.smartrecruiters.com/<company>)"
    )


def _location_of(posting: Dict[str, Any]) -> str:
    """Assemble a readable location from a SmartRecruiters posting.

    Args:
        posting: One entry of the API's ``content`` list.

    Returns:
        ``"City, Region, Country"`` with missing parts omitted, or ``""``.
    """
    location = posting.get("location")
    if not isinstance(location, dict):
        return ""

    parts = [
        str(location.get("city") or "").strip(),
        str(location.get("region") or "").strip(),
        str(location.get("country") or "").strip().upper(),
    ]
    return ", ".join(part for part in parts if part)


def _country_of(posting: Dict[str, Any]) -> str:
    """Read the country from a SmartRecruiters posting.

    The API reports an ISO alpha-2 code, which is left to
    :func:`utils.location.derive_country` to interpret unless it is one of the
    two codes that appear constantly in this data set.

    Args:
        posting: One entry of the API's ``content`` list.

    Returns:
        A country name, or ``""`` to let the shared derivation decide.
    """
    location = posting.get("location")
    if not isinstance(location, dict):
        return ""

    code = str(location.get("country") or "").strip().lower()
    return {"us": "United States", "gb": "United Kingdom", "uk": "United Kingdom"}.get(code, "")


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a SmartRecruiters board.

    Args:
        career_url: Any SmartRecruiters board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names no company.
        AdapterHttpError: If the board cannot be read.
    """
    company_id = parse_company_id(career_url)
    api_url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
    board_url = f"https://jobs.smartrecruiters.com/{company_id}"

    logger.info("SmartRecruiters: company {!r} for {!r}", company_id, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    collected: List[Optional[Job]] = []
    offset = 0
    total: Optional[int] = None

    try:
        for page in range(1, _MAX_PAGES + 1):
            payload = get_json(http, api_url, params={"offset": offset, "limit": _PAGE_SIZE})

            if not isinstance(payload, dict):
                raise AdapterHttpError(
                    f"SmartRecruiters returned {type(payload).__name__} for {api_url}"
                )

            postings = payload.get("content")
            if not isinstance(postings, list):
                raise AdapterHttpError(f"SmartRecruiters returned no content list for {api_url}")

            if total is None:
                raw_total = payload.get("totalFound")
                total = raw_total if isinstance(raw_total, int) and raw_total >= 0 else None

            if not postings:
                break

            for posting in postings:
                if not isinstance(posting, dict):
                    continue
                posting_id = str(posting.get("id") or "").strip()
                collected.append(
                    build_job(
                        company_name=company_name,
                        title=str(posting.get("name") or ""),
                        job_url=f"{board_url}/{posting_id}" if posting_id else "",
                        location=_location_of(posting),
                        country=_country_of(posting),
                        career_page_url=board_url,
                        platform=PLATFORM,
                    )
                )

            offset += len(postings)
            logger.debug("SmartRecruiters: page {} gave {} posting(s)", page, len(postings))

            if total is not None and offset >= total:
                break
            if len(postings) < _PAGE_SIZE:
                break
        else:
            raise AdapterHttpError(
                f"SmartRecruiters paging did not terminate for {api_url} after {_MAX_PAGES} pages"
            )
    finally:
        if owned:
            http.close()

    jobs = dedupe(collected)
    logger.success("SmartRecruiters: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
