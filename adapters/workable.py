"""Extract job postings from Workable job boards.

Workable serves each account's published jobs through the widget API its own
embed uses, which needs no credentials and returns the whole board at once::

    GET https://apply.workable.com/api/v1/widget/accounts/<account>?details=true

Some accounts answer only on the newer paginated endpoint, so that is used as a
fallback::

    POST https://apply.workable.com/api/v3/accounts/<account>/jobs

The account slug is the first path segment of ``apply.workable.com/<account>``.
"""

from __future__ import annotations

from typing import Any, Dict, Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.http import AdapterError, AdapterHttpError, AdapterUrlError, build_session, get_json, post_json
from utils.jobs import build_job, dedupe

__all__ = ["PLATFORM", "fetch_jobs", "parse_account_slug"]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Workable"

#: Hard stop on v3 pagination, far above any real board.
_MAX_PAGES: Final[int] = 200

#: Path segments belonging to Workable's routing rather than an account.
#: ``search``, ``browse`` and ``companies`` are Workable's own public job
#: aggregator — ``jobs.workable.com/search?location=...`` lists postings from
#: every customer, not from one. Treating that as an account name attributed a
#: stranger's postings to whichever company the sheet had pasted it against,
#: which is worse than reporting nothing.
_RESERVED: Final[frozenset] = frozenset(
    {"api", "v1", "v3", "widget", "accounts", "j", "jobs", "search", "browse", "companies"}
)

#: Host labels Workable shares across every customer. On these the account is
#: the first path segment; on any other label the host *is* the account.
_SHARED_HOSTS: Final[frozenset] = frozenset({"apply", "www", "jobs", "careers"})


def parse_account_slug(career_url: str) -> str:
    """Read the account slug out of a Workable URL.

    Workable publishes boards under two layouts, and which one it is decides
    where the account name lives::

        https://apply.workable.com/<account>/j/<code>/   # shared host, account in the path
        https://<account>.workable.com/jobs/<id>         # tenant host, account in the host

    The host is therefore checked first. Reading the path first would take the
    job id out of the second shape and query a board that does not exist —
    ``acme.workable.com/jobs/12345`` would look up the account ``12345``.

    Args:
        career_url: A Workable board or posting URL.

    Returns:
        The account slug.

    Raises:
        AdapterUrlError: If the URL names no account.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No Workable URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable Workable URL: {career_url!r} ({exc})") from exc

    label = (parts.hostname or "").lower().split(".")[0]
    if label and label not in _SHARED_HOSTS:
        return label

    for segment in (segment for segment in parts.path.split("/") if segment):
        if segment.lower() in _RESERVED:
            continue
        return segment

    raise AdapterUrlError(
        f"No Workable account in {career_url!r} "
        "(expected something like https://apply.workable.com/<account>/)"
    )


def _location_from_parts(*parts: Any) -> str:
    """Join location components, dropping the empty ones.

    Args:
        *parts: City, region and country in display order.

    Returns:
        A comma-separated location, or ``""``.
    """
    cleaned = [str(part or "").strip() for part in parts]
    return ", ".join(part for part in cleaned if part)


def _jobs_from_widget(
    payload: Any, company_name: str, board_url: str
) -> List[Optional[Job]]:
    """Convert a v1 widget response into jobs.

    Args:
        payload: The decoded widget response.
        company_name: Company as named in the input sheet.
        board_url: Board URL, recorded on each job.

    Returns:
        One entry per posting; unusable postings are ``None``.

    Raises:
        AdapterHttpError: If the payload carries no jobs list.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise AdapterHttpError("Workable widget response carried no jobs list")

    collected: List[Optional[Job]] = []
    for posting in payload["jobs"]:
        if not isinstance(posting, dict):
            continue

        collected.append(
            build_job(
                company_name=company_name,
                title=str(posting.get("title") or ""),
                job_url=str(posting.get("url") or posting.get("application_url") or ""),
                location=_location_from_parts(
                    posting.get("city"), posting.get("state"), posting.get("country")
                ),
                country=str(posting.get("country") or "").strip(),
                career_page_url=board_url,
                platform=PLATFORM,
            )
        )

    return collected


def _jobs_from_v3(
    session: requests.Session, account: str, company_name: str, board_url: str
) -> List[Optional[Job]]:
    """Walk the paginated v3 endpoint.

    Args:
        session: Session to use.
        account: Workable account slug.
        company_name: Company as named in the input sheet.
        board_url: Board URL, recorded on each job.

    Returns:
        One entry per posting; unusable postings are ``None``.

    Raises:
        AdapterHttpError: If the endpoint cannot be read or does not terminate.
    """
    api_url = f"https://apply.workable.com/api/v3/accounts/{account}/jobs"
    collected: List[Optional[Job]] = []
    token: Optional[str] = None

    for page in range(1, _MAX_PAGES + 1):
        body: Dict[str, Any] = {"query": "", "location": [], "department": [], "worktype": []}
        if token:
            body["token"] = token

        payload = post_json(session, api_url, body)
        if not isinstance(payload, dict):
            raise AdapterHttpError(f"Workable v3 returned {type(payload).__name__} for {api_url}")

        results = payload.get("results")
        if not isinstance(results, list):
            raise AdapterHttpError(f"Workable v3 returned no results list for {api_url}")

        for posting in results:
            if not isinstance(posting, dict):
                continue

            shortcode = str(posting.get("shortcode") or "").strip()
            locations = posting.get("locations")
            location = ""
            if isinstance(locations, list) and locations:
                first = locations[0]
                if isinstance(first, dict):
                    location = _location_from_parts(
                        first.get("city"), first.get("region"), first.get("country")
                    )

            collected.append(
                build_job(
                    company_name=company_name,
                    title=str(posting.get("title") or ""),
                    job_url=f"{board_url}/j/{shortcode}" if shortcode else "",
                    location=location,
                    career_page_url=board_url,
                    platform=PLATFORM,
                )
            )

        logger.debug("Workable: v3 page {} gave {} posting(s)", page, len(results))

        token = payload.get("nextPage") or None
        if not token or not results:
            return collected

    raise AdapterHttpError(f"Workable v3 paging did not terminate for {api_url}")


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a Workable board.

    The widget endpoint is tried first because it returns the whole board in one
    request; accounts that do not answer there fall through to the paginated v3
    endpoint.

    Args:
        career_url: Any Workable board or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting on the board, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` names no account.
        AdapterHttpError: If neither endpoint can be read.
    """
    account = parse_account_slug(career_url)
    board_url = f"https://apply.workable.com/{account}"
    widget_url = f"https://apply.workable.com/api/v1/widget/accounts/{account}"

    logger.info("Workable: account {!r} for {!r}", account, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        try:
            payload = get_json(http, widget_url, params={"details": "true"})
            collected = _jobs_from_widget(payload, company_name, board_url)
        except AdapterError as exc:
            logger.debug("Workable: widget endpoint unusable ({}), trying v3", exc)
            collected = _jobs_from_v3(http, account, company_name, board_url)
    finally:
        if owned:
            http.close()

    jobs = dedupe(collected)
    logger.success("Workable: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
