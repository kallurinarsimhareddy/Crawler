"""Extract job postings from Workday job boards.

Workday boards render their listings client-side, so scraping the HTML yields
an empty shell. Every board is however backed by the same public "CXS" JSON
endpoint that the page itself calls, and it needs no authentication::

    POST https://<host>/wday/cxs/<tenant>/<site>/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

which answers with a page of postings plus the board's total::

    {"total": 137, "jobPostings": [{"title": ..., "externalPath": ...,
                                    "locationsText": ...}, ...]}

This module turns a board URL into that endpoint, walks every page, and returns
one :class:`models.job.Job` per posting::

    >>> from adapters.workday import fetch_jobs
    >>> jobs = fetch_jobs("https://acme.wd1.myworkdayjobs.com/en-US/External", "Acme")
    >>> jobs[0].job_title
    'Senior Platform Engineer'

Pagination runs to exhaustion — nothing is truncated, and no result cap is
applied. Transport failures are retried with exponential backoff; anything that
survives the retries is raised as a :class:`WorkdayError` naming the board and
the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Final, List, Optional, Sequence
from urllib.parse import urlsplit

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from models.job import Job
from utils.location import derive_country

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "PLATFORM",
    "WorkdayApiError",
    "WorkdayBoard",
    "WorkdayError",
    "WorkdayUrlError",
    "build_session",
    "fetch_jobs",
    "parse_board_url",
]

#: Label written to the ``Platform`` column for every posting from this adapter.
PLATFORM: Final[str] = "Workday"

#: Postings requested per call. Workday caps a page at 20 and silently clamps
#: anything larger, so asking for more only wastes the round trip.
DEFAULT_PAGE_SIZE: Final[int] = 20

#: Seconds to wait for connect and for read, respectively.
DEFAULT_TIMEOUT: Final[tuple[float, float]] = (10.0, 30.0)

#: Attempts per request, including the first. Workday rate-limits aggressively
#: on large boards, and a 429 mid-crawl must not lose the run.
DEFAULT_RETRIES: Final[int] = 4

#: Statuses worth retrying: rate limiting and transient upstream failures.
_RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

#: Domains Workday serves boards from.
_WORKDAY_HOSTS: Final[tuple[str, ...]] = (
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "myworkday.com",
)

#: Locale segment in a board path, e.g. "en-US" or "fr". The language subtag is
#: matched case-sensitively: real locales lowercase it, whereas short site names
#: are capitalised ("VR", "WG"), and those must not be mistaken for a locale.
_LANGUAGE: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2})?$")

#: Default locale, used when a board URL carries none.
_DEFAULT_LANGUAGE: Final[str] = "en-US"

#: Presented as a browser because some tenants reject unknown clients outright.
_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

#: Hard stop on pagination. At 20 postings a page this allows 100,000 postings —
#: far beyond any real board, so it only ever fires on a tenant whose paging is
#: broken, in place of looping forever.
_MAX_PAGES: Final[int] = 5_000


class WorkdayError(Exception):
    """Base class for every failure raised by this adapter."""


class WorkdayUrlError(WorkdayError, ValueError):
    """The URL is not a Workday board, or its tenant and site are unreadable."""


class WorkdayApiError(WorkdayError, RuntimeError):
    """The Workday API could not be reached, or answered unusably."""


@dataclass(frozen=True)
class WorkdayBoard:
    """The three coordinates that identify a Workday job board.

    Attributes:
        host: Board hostname, e.g. ``"acme.wd1.myworkdayjobs.com"``.
        tenant: Workday tenant, e.g. ``"acme"``.
        site: Career site name, e.g. ``"External"``.
        language: Locale segment used when building posting URLs.
    """

    host: str
    tenant: str
    site: str
    language: str = _DEFAULT_LANGUAGE

    @property
    def api_url(self) -> str:
        """URL of the board's CXS jobs endpoint."""
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    @property
    def board_url(self) -> str:
        """Public URL of the board, and the base for each posting's URL."""
        return f"https://{self.host}/{self.language}/{self.site}"

    def job_url(self, external_path: str) -> str:
        """Build the public URL of one posting.

        Args:
            external_path: The posting's ``externalPath``, e.g.
                ``"/job/Austin/Platform-Engineer_R-1234"``.

        Returns:
            The absolute posting URL, or ``""`` if ``external_path`` is empty.
        """
        if not external_path:
            return ""

        return f"{self.board_url}/{external_path.lstrip('/')}"


def _host_is_workday(host: str) -> bool:
    """Report whether ``host`` belongs to Workday.

    Args:
        host: Lowercased hostname.

    Returns:
        ``True`` if the host is one of Workday's domains or a subdomain of one.
    """
    return any(host == domain or host.endswith(f".{domain}") for domain in _WORKDAY_HOSTS)


def parse_board_url(url: str) -> WorkdayBoard:
    """Read tenant, site and locale out of a Workday URL.

    Accepts every shape a board URL arrives in:

    * ``https://acme.wd1.myworkdayjobs.com/External``
    * ``https://acme.wd1.myworkdayjobs.com/en-US/External``
    * ``https://acme.wd5.myworkdaysite.com/en-US/recruiting/acme/External``
    * ``https://acme.wd1.myworkdayjobs.com/en-US/External/job/Austin/Engineer_R-1``
    * ``https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs``

    Args:
        url: A Workday board, posting or API URL.

    Returns:
        The parsed :class:`WorkdayBoard`.

    Raises:
        WorkdayUrlError: If ``url`` is unparsable, is not hosted by Workday, or
            carries no career site name.
    """
    if not url or not str(url).strip():
        raise WorkdayUrlError("No Workday URL supplied")

    raw = str(url).strip()
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise WorkdayUrlError(f"Unparsable Workday URL: {url!r} ({exc})") from exc

    if not host:
        raise WorkdayUrlError(f"Workday URL has no hostname: {url!r}")
    if not _host_is_workday(host):
        raise WorkdayUrlError(
            f"Not a Workday URL: {url!r} (host {host!r} is not one of {', '.join(_WORKDAY_HOSTS)})"
        )

    segments = [segment for segment in parts.path.split("/") if segment]

    # The API form already names tenant and site: /wday/cxs/<tenant>/<site>/jobs
    if len(segments) >= 4 and segments[0] == "wday" and segments[1] == "cxs":
        return WorkdayBoard(host=host, tenant=segments[2], site=segments[3])

    # A posting URL carries the board prefix; everything from /job/ is detail.
    for index, segment in enumerate(segments):
        if segment.lower() == "job":
            segments = segments[:index]
            break

    # A locale only ever precedes the site, so the leading segment can only be
    # one if something follows it. Without this, the sole segment of
    # ".../VR?jobFamilyGroup=..." is eaten as a locale and the site is lost.
    language = _DEFAULT_LANGUAGE
    if len(segments) > 1 and _LANGUAGE.match(segments[0]):
        language = segments.pop(0)

    # myworkdaysite.com nests the board under /recruiting/<tenant>/<site>.
    if segments and segments[0].lower() == "recruiting":
        segments.pop(0)
        if len(segments) < 2:
            raise WorkdayUrlError(
                f"Workday URL is missing tenant or site after /recruiting/: {url!r}"
            )
        return WorkdayBoard(host=host, tenant=segments[0], site=segments[1], language=language)

    if not segments:
        raise WorkdayUrlError(
            f"Workday URL names no career site: {url!r} "
            "(expected something like https://<tenant>.wd1.myworkdayjobs.com/en-US/<site>)"
        )

    # Otherwise the tenant is the leading hostname label and the site the path.
    tenant = host.split(".")[0]
    return WorkdayBoard(host=host, tenant=tenant, site=segments[0], language=language)


def build_session(retries: int = DEFAULT_RETRIES) -> requests.Session:
    """Create a session that retries transient failures with backoff.

    Retries cover connection errors, read errors and the statuses in
    :data:`_RETRY_STATUSES`, for POST as well as GET. ``Retry-After`` is
    honoured when Workday sends it.

    Args:
        retries: Total attempts per request, including the first. Values below
            ``1`` are treated as ``1`` (no retrying).

    Returns:
        A configured session. The caller owns it and should close it.
    """
    attempts = max(1, int(retries))

    policy = Retry(
        total=attempts - 1,
        connect=attempts - 1,
        read=attempts - 1,
        status=attempts - 1,
        status_forcelist=sorted(_RETRY_STATUSES),
        allowed_methods=frozenset({"GET", "POST"}),
        backoff_factor=1.0,
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )

    adapter = HTTPAdapter(max_retries=policy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _request_page(
    session: requests.Session,
    board: WorkdayBoard,
    offset: int,
    limit: int,
    timeout: tuple[float, float],
) -> Dict[str, Any]:
    """Fetch one page of postings.

    Args:
        session: Session to use, normally from :func:`build_session`.
        board: Board being crawled.
        offset: Index of the first posting to return.
        limit: Maximum postings to return.
        timeout: ``(connect, read)`` timeout in seconds.

    Returns:
        The decoded response body.

    Raises:
        WorkdayApiError: On a transport failure, a non-2xx status that survived
            retries, or a body that is not a JSON object.
    """
    payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}

    try:
        response = session.post(board.api_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise WorkdayApiError(
            f"Workday request failed for {board.api_url} at offset {offset}: {exc}"
        ) from exc

    if response.status_code == 404:
        raise WorkdayApiError(
            f"Workday board not found: {board.api_url} "
            f"(tenant {board.tenant!r}, site {board.site!r} — check the board URL)"
        )
    if not response.ok:
        raise WorkdayApiError(
            f"Workday returned HTTP {response.status_code} for {board.api_url} "
            f"at offset {offset}: {response.text[:200]!r}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise WorkdayApiError(
            f"Workday returned a non-JSON body for {board.api_url} at offset {offset}: "
            f"{response.text[:200]!r}"
        ) from exc

    if not isinstance(body, dict):
        raise WorkdayApiError(
            f"Workday returned {type(body).__name__}, expected an object, "
            f"for {board.api_url} at offset {offset}"
        )

    return body


def _text(value: Any) -> str:
    """Coerce a JSON value to a stripped string.

    Args:
        value: Any value taken from the response body.

    Returns:
        The stripped string form, or ``""`` for ``None``.
    """
    if value is None:
        return ""
    return str(value).strip()


def _to_job(posting: Dict[str, Any], board: WorkdayBoard, company_name: str) -> Optional[Job]:
    """Convert one raw posting into a :class:`~models.job.Job`.

    Args:
        posting: A single entry of the response's ``jobPostings``.
        board: Board the posting came from, used to build its URL.
        company_name: Company as named in the input sheet.

    Returns:
        The job, or ``None`` if the posting has no title or no resolvable URL
        and so could not be delivered to a candidate anyway.
    """
    title = _text(posting.get("title"))
    job_url = board.job_url(_text(posting.get("externalPath")))

    if not title or not job_url:
        logger.debug(
            "Skipping malformed posting on {} (title={!r}, externalPath={!r})",
            board.api_url,
            posting.get("title"),
            posting.get("externalPath"),
        )
        return None

    location = _text(posting.get("locationsText"))

    return Job(
        company_name=company_name,
        job_title=title,
        job_url=job_url,
        location=location,
        country=derive_country(location),
        career_page_url=board.board_url,
        platform=PLATFORM,
    )


def _postings_of(body: Dict[str, Any], board: WorkdayBoard) -> Sequence[Dict[str, Any]]:
    """Pull the postings list out of a response body.

    Args:
        body: A decoded response body.
        board: Board being crawled, for the error message.

    Returns:
        The postings, or an empty sequence when the board reports none.

    Raises:
        WorkdayApiError: If ``jobPostings`` is present but not a list.
    """
    postings = body.get("jobPostings")
    if postings is None:
        return ()
    if not isinstance(postings, list):
        raise WorkdayApiError(
            f"Workday returned jobPostings as {type(postings).__name__}, expected a list, "
            f"for {board.api_url}"
        )

    return [posting for posting in postings if isinstance(posting, dict)]


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> List[Job]:
    """Fetch every posting on a Workday board.

    Pages through the board's CXS endpoint until it stops returning postings,
    so the result is the complete board — no cap and no truncation. Duplicate
    postings, which Workday emits when one job is filed under several
    categories, are removed.

    Args:
        career_url: Any Workday board, posting or API URL for the company.
        company_name: Company as named in the input sheet; copied onto every
            returned job.
        session: Session to reuse across calls. When omitted, one is built via
            :func:`build_session` and closed before returning.
        page_size: Postings requested per call. Workday clamps this to 20.
        timeout: ``(connect, read)`` timeout in seconds.
        retries: Attempts per request, including the first. Ignored when
            ``session`` is supplied, since the caller's retry policy governs.

    Returns:
        Every posting on the board, in board order. An empty list if the board
        is live but currently advertises nothing.

    Raises:
        WorkdayUrlError: If ``career_url`` is not a usable Workday board URL.
        WorkdayApiError: If the board cannot be read after retries.
    """
    board = parse_board_url(career_url)
    limit = max(1, min(int(page_size), DEFAULT_PAGE_SIZE))

    logger.info(
        "Workday: crawling tenant {!r} site {!r} for {!r} ({})",
        board.tenant,
        board.site,
        company_name,
        board.api_url,
    )

    owned_session = session is None
    http = session if session is not None else build_session(retries=retries)

    jobs: List[Job] = []
    seen: set = set()
    offset = 0
    total: Optional[int] = None
    skipped = 0

    try:
        for page in range(1, _MAX_PAGES + 1):
            body = _request_page(http, board, offset=offset, limit=limit, timeout=timeout)
            postings = _postings_of(body, board)

            if total is None:
                raw_total = body.get("total")
                total = raw_total if isinstance(raw_total, int) and raw_total >= 0 else None
                logger.debug(
                    "Workday: {!r} reports {} posting(s) on {}",
                    company_name,
                    total if total is not None else "an unstated number of",
                    board.site,
                )

            if not postings:
                logger.debug("Workday: page {} returned no postings, board exhausted", page)
                break

            new_on_page = 0
            for posting in postings:
                job = _to_job(posting, board, company_name)
                if job is None:
                    skipped += 1
                    continue
                if job.key in seen:
                    continue
                seen.add(job.key)
                jobs.append(job)
                new_on_page += 1

            logger.debug(
                "Workday: page {} gave {} posting(s), {} new (running total {})",
                page,
                len(postings),
                new_on_page,
                len(jobs),
            )

            offset += len(postings)

            # A tenant that ignores `offset` replays page one forever. Every
            # posting already being known is the signal, and it is safe: a page
            # of genuinely new jobs always yields at least one addition.
            if new_on_page == 0:
                logger.warning(
                    "Workday: page {} for {!r} repeated known postings, stopping to avoid a loop",
                    page,
                    company_name,
                )
                break

            if total is not None and offset >= total:
                break
        else:
            raise WorkdayApiError(
                f"Workday paging did not terminate for {board.api_url} after {_MAX_PAGES} pages "
                f"({len(jobs)} job(s) collected); the tenant is likely ignoring 'offset'"
            )
    finally:
        if owned_session:
            http.close()

    if skipped:
        logger.warning("Workday: skipped {} malformed posting(s) for {!r}", skipped, company_name)

    if total is not None and len(jobs) < total:
        # Not an error: boards shrink mid-crawl as postings close, and Workday
        # counts postings that are filtered from the public list.
        logger.warning(
            "Workday: collected {} of {} reported posting(s) for {!r} ({})",
            len(jobs),
            total,
            company_name,
            board.api_url,
        )

    logger.success("Workday: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
