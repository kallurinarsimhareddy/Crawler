"""Extract job postings from UKG / UltiPro Recruiting job boards.

An UltiPro board is a single-page app whose listings come from the paginated
endpoint the page itself calls::

    POST https://recruiting.ultipro.com/<tenant>/JobBoard/<board-id>/JobBoardView/LoadSearchResults
    {"opportunitySearch": {"Top": 100, "Skip": 0, ...}}

Tenant and board id are the first two path segments after the host, and each
posting is addressed as ``OpportunityDetail?opportunityId=<guid>``.
"""

from __future__ import annotations

from typing import Any, Dict, Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.http import AdapterHttpError, AdapterUrlError, build_session, post_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "UltiProBoard", "fetch_jobs", "parse_board_url"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "UltiPro"

#: Postings requested per call.
_PAGE_SIZE: Final[int] = 100

#: Hard stop on pagination, far above any real board.
_MAX_PAGES: Final[int] = 200


class UltiProBoard:
    """The host, tenant and board id that address one UltiPro job board.

    Attributes:
        host: Board hostname, e.g. ``"recruiting.ultipro.com"``.
        tenant: Tenant code, e.g. ``"AGR1003ARGI"``.
        board_id: Board GUID.
    """

    __slots__ = ("host", "tenant", "board_id")

    def __init__(self, host: str, tenant: str, board_id: str) -> None:
        self.host = host
        self.tenant = tenant
        self.board_id = board_id

    @property
    def board_url(self) -> str:
        """Public URL of the board."""
        return f"https://{self.host}/{self.tenant}/JobBoard/{self.board_id}"

    @property
    def api_url(self) -> str:
        """URL of the board's search endpoint."""
        return f"{self.board_url}/JobBoardView/LoadSearchResults"

    def job_url(self, opportunity_id: str) -> str:
        """Build the public URL of one posting.

        Args:
            opportunity_id: The posting's ``Id``.

        Returns:
            The absolute posting URL, or ``""`` without an id.
        """
        if not opportunity_id:
            return ""
        return f"{self.board_url}/OpportunityDetail?opportunityId={opportunity_id}"


def parse_board_url(career_url: str) -> UltiProBoard:
    """Read host, tenant and board id out of an UltiPro URL.

    Args:
        career_url: An UltiPro board or posting URL.

    Returns:
        The parsed :class:`UltiProBoard`.

    Raises:
        AdapterUrlError: If tenant or board id cannot be read.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No UltiPro URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable UltiPro URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise AdapterUrlError(f"UltiPro URL has no hostname: {career_url!r}")

    segments = [segment for segment in parts.path.split("/") if segment]

    for index, segment in enumerate(segments):
        if segment.lower() == "jobboard" and index >= 1 and index + 1 < len(segments):
            return UltiProBoard(host=host, tenant=segments[index - 1], board_id=segments[index + 1])

    raise AdapterUrlError(
        f"No UltiPro tenant and board id in {career_url!r} "
        "(expected https://recruiting.ultipro.com/<tenant>/JobBoard/<board-id>)"
    )


def _location_of(opportunity: Dict[str, Any]) -> str:
    """Assemble a readable location from an UltiPro opportunity.

    Args:
        opportunity: One entry of the endpoint's ``opportunities`` list.

    Returns:
        The location of the first listed site, with a count appended when the
        posting spans several, or ``""``.
    """
    locations = opportunity.get("Locations")
    if not isinstance(locations, list) or not locations:
        return ""

    first = locations[0]
    if not isinstance(first, dict):
        return ""

    described = str(first.get("LocalizedDescription") or "").strip()
    if not described:
        address = first.get("Address")
        if isinstance(address, dict):
            state = address.get("State")
            country = address.get("Country")
            parts = [
                str(address.get("City") or "").strip(),
                str(state.get("Code") or state.get("Name") or "").strip()
                if isinstance(state, dict)
                else "",
                str(country.get("Name") or "").strip() if isinstance(country, dict) else "",
            ]
            described = ", ".join(part for part in parts if part)

    if len(locations) > 1 and described:
        return f"{described} (+{len(locations) - 1} more)"

    return described


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an UltiPro board.

    Args:
        career_url: Any UltiPro board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names no board.
        AdapterHttpError: If the board cannot be read.
    """
    board = parse_board_url(career_url)

    logger.info("UltiPro: tenant {!r} board {!r} for {!r}", board.tenant, board.board_id, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    collected: List[Optional[Job]] = []
    skip = 0
    total: Optional[int] = None

    try:
        for page in range(1, _MAX_PAGES + 1):
            body = {
                "opportunitySearch": {
                    "Top": _PAGE_SIZE,
                    "Skip": skip,
                    "QueryString": "",
                    "OrderBy": [
                        {
                            "Value": "postedDateDesc",
                            "PropertyName": "PostedDate",
                            "Ascending": False,
                        }
                    ],
                    "Filters": [],
                },
                "matchCriteria": {
                    "PreferredJobs": [],
                    "Educations": [],
                    "LicenseAndCertifications": [],
                    "Skills": [],
                    "hasNoLicenses": False,
                    "SkippedSkills": [],
                },
            }

            payload = post_json(http, board.api_url, body, headers={"Referer": board.board_url})

            if not isinstance(payload, dict):
                raise AdapterHttpError(
                    f"UltiPro returned {type(payload).__name__} for {board.api_url}"
                )

            opportunities = payload.get("opportunities")
            if not isinstance(opportunities, list):
                raise AdapterHttpError(
                    f"UltiPro returned no opportunities list for {board.api_url}"
                )

            if total is None:
                raw_total = payload.get("totalCount")
                total = raw_total if isinstance(raw_total, int) and raw_total >= 0 else None

            if not opportunities:
                break

            for opportunity in opportunities:
                if not isinstance(opportunity, dict):
                    continue

                collected.append(
                    build_job(
                        company_name=company_name,
                        title=str(opportunity.get("Title") or ""),
                        job_url=board.job_url(str(opportunity.get("Id") or "").strip()),
                        location=_location_of(opportunity),
                        career_page_url=board.board_url,
                        platform=PLATFORM,
                    )
                )

            skip += len(opportunities)
            logger.debug("UltiPro: page {} gave {} opportunity(ies)", page, len(opportunities))

            if total is not None and skip >= total:
                break
            if len(opportunities) < _PAGE_SIZE:
                break
        else:
            raise AdapterHttpError(
                f"UltiPro paging did not terminate for {board.api_url} after {_MAX_PAGES} pages"
            )
    finally:
        if owned:
            http.close()

    jobs = dedupe(collected)
    logger.success("UltiPro: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
