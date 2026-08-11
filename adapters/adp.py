"""Extract job postings from ADP Workforce Now career centres.

The ADP career centre is a single-page app fed by a public staffing endpoint
that takes the client id from the page URL::

    GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions
        ?cid=<cid>&locale=en_US&$top=50&$skip=0

``cid`` (and the optional ``ccId``) come from the query string of the
recruitment URL in the input sheet; both are needed to address a posting.

ADP also operates a second, unrelated product at ``myjobs.adp.com``. Its
listings are not served by this endpoint, so those URLs are rejected with a
message saying so rather than silently returning nothing.
"""

from __future__ import annotations

from typing import Any, Dict, Final, List, Optional
from urllib.parse import parse_qs, urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_client_ids"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "ADP"

#: Host serving the Workforce Now career centre.
_HOST: Final[str] = "workforcenow.adp.com"

#: Endpoint backing the career centre.
_API_PATH: Final[str] = "/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"

#: Page size the endpoint accepts.
_PAGE_SIZE: Final[int] = 50

#: Hard stop on pagination, far above any real career centre.
_MAX_PAGES: Final[int] = 200


def parse_client_ids(career_url: str) -> tuple[str, str]:
    """Read the ADP client and career-centre ids out of a recruitment URL.

    Args:
        career_url: An ADP Workforce Now recruitment URL.

    Returns:
        ``(cid, cc_id)``; ``cc_id`` is ``""`` when the URL does not carry one.

    Raises:
        AdapterUrlError: If the URL carries no ``cid``, or points at the
            unrelated ``myjobs.adp.com`` product.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No ADP URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable ADP URL: {career_url!r} ({exc})") from exc

    host = (parts.hostname or "").lower()
    if "myjobs.adp.com" in host:
        raise AdapterUrlError(
            f"{career_url!r} is an ADP Recruiting Management board (myjobs.adp.com), which is "
            "served by a different, non-public API than the Workforce Now career centre"
        )

    query = parse_qs(parts.query)
    cid = (query.get("cid") or [""])[0].strip()
    cc_id = (query.get("ccId") or query.get("ccid") or [""])[0].strip()

    if not cid:
        raise AdapterUrlError(
            f"No ADP client id in {career_url!r} "
            "(expected a 'cid' query parameter on the recruitment URL)"
        )

    return cid, cc_id


def _location_of(requisition: Dict[str, Any]) -> str:
    """Assemble a readable location from an ADP requisition.

    Args:
        requisition: One entry of the endpoint's ``jobRequisitions`` list.

    Returns:
        The first listed location, with a count appended when there are more.
    """
    locations = requisition.get("requisitionLocations")
    if not isinstance(locations, list) or not locations:
        return ""

    described: List[str] = []
    for entry in locations:
        if not isinstance(entry, dict):
            continue

        address = entry.get("address") if isinstance(entry.get("address"), dict) else entry
        city = address.get("cityName") or ""
        subdivision = address.get("countrySubdivisionLevel1")
        state = subdivision.get("codeValue") if isinstance(subdivision, dict) else ""
        country = address.get("countryCode") or ""

        name_code = entry.get("nameCode")
        short = name_code.get("shortName") if isinstance(name_code, dict) else ""

        parts = [str(city).strip(), str(state or "").strip(), str(country or "").strip()]
        joined = ", ".join(part for part in parts if part) or str(short or "").strip()
        if joined:
            described.append(joined)

    if not described:
        return ""
    if len(described) > 1:
        return f"{described[0]} (+{len(described) - 1} more)"
    return described[0]


def _requisitions_of(payload: Any, api_url: str) -> List[Dict[str, Any]]:
    """Pull the requisition list out of a response body.

    Args:
        payload: The decoded response.
        api_url: URL, for the error message.

    Returns:
        The requisitions on this page.

    Raises:
        AdapterHttpError: If the body carries no requisition list.
    """
    if not isinstance(payload, dict):
        raise AdapterHttpError(f"ADP returned {type(payload).__name__} for {api_url}")

    requisitions = payload.get("jobRequisitions")
    if requisitions is None:
        return []
    if not isinstance(requisitions, list):
        raise AdapterHttpError(f"ADP returned no jobRequisitions list for {api_url}")

    return [item for item in requisitions if isinstance(item, dict)]


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting in an ADP Workforce Now career centre.

    Args:
        career_url: An ADP recruitment URL carrying a ``cid``.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every open requisition, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` carries no client id.
        AdapterHttpError: If the career centre cannot be read.
    """
    cid, cc_id = parse_client_ids(career_url)
    api_url = f"https://{_HOST}{_API_PATH}"
    board_url = (
        f"https://{_HOST}/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}"
        + (f"&ccId={cc_id}" if cc_id else "")
        + "&lang=en_US"
    )

    logger.info("ADP: client {!r} for {!r}", cid, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    collected: List[Optional[Job]] = []
    skip = 0

    try:
        for page in range(1, _MAX_PAGES + 1):
            payload = get_json(
                http,
                api_url,
                params={
                    "cid": cid,
                    "locale": "en_US",
                    "$top": _PAGE_SIZE,
                    "$skip": skip,
                },
                headers={"Referer": board_url},
            )

            requisitions = _requisitions_of(payload, api_url)
            if not requisitions:
                break

            for requisition in requisitions:
                item_id = str(requisition.get("itemID") or requisition.get("itemId") or "").strip()
                job_url = (
                    f"https://{_HOST}/mascsr/default/mdf/recruitment/recruitment.html"
                    f"?cid={cid}" + (f"&ccId={cc_id}" if cc_id else "") + f"&jobId={item_id}&lang=en_US"
                    if item_id
                    else ""
                )

                collected.append(
                    build_job(
                        company_name=company_name,
                        title=str(requisition.get("requisitionTitle") or ""),
                        job_url=job_url,
                        location=_location_of(requisition),
                        career_page_url=board_url,
                        platform=PLATFORM,
                    )
                )

            skip += len(requisitions)
            logger.debug("ADP: page {} gave {} requisition(s)", page, len(requisitions))

            if len(requisitions) < _PAGE_SIZE:
                break
        else:
            raise AdapterHttpError(
                f"ADP paging did not terminate for {api_url} after {_MAX_PAGES} pages"
            )
    finally:
        if owned:
            http.close()

    jobs = dedupe(collected)
    logger.success("ADP: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
