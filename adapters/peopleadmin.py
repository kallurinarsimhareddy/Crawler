"""Extract job postings from PeopleAdmin boards.

PeopleAdmin is a higher-education applicant tracking system. Its boards page
server-side through a search view::

    https://<tenant>/postings/search
    https://<tenant>/postings/search?page=2

**The hostname proves nothing.** Institutions front PeopleAdmin with their own
domain — ``jobs.montana.edu``, ``employment.plu.edu`` — so a host rule finds
almost none of them. What identifies a board is its markup: a
``#job_list_header_responsive`` row naming the columns, a ``#search_results``
container, and ``/postings/<id>`` links inside it.

**Each tenant configures its own result columns**, and the row cells carry no
labels of their own. Montana State publishes Posting Number, Division,
Department, Position Type and Job Close Date; Pacific Lutheran publishes Job
Open Date, Position Type and Department — a different set, in a different
order, with the shared columns in different positions. Reading either by
position would put one board's date into the other's department. So the header
is parsed first and the row cells are matched to it by name, and a column this
module has no field for is passed over rather than guessed at.

Nothing is inferred. Neither board publishes a location, so ``location`` stays
empty rather than being filled from a division or a campus name.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Dict, Final, List, Optional
from urllib.parse import urljoin, urlsplit

import requests
from loguru import logger

from models.job import Job
from utils.html import absolute_url, clean_text, parse_html
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_text
from utils.jobs import build_job, dedupe

__all__ = [
    "MAX_PAGES",
    "PLATFORM",
    "fetch_jobs",
    "looks_like_a_board",
    "parse_board_url",
]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "PeopleAdmin"

#: The search view every board pages through. Taken from the boards' own
#: navigation and pagination links, not invented.
SEARCH_PATH: Final[str] = "/postings/search"

#: Result pages to walk before giving up.
MAX_PAGES: Final[int] = 40

#: A link to one posting.
_POSTING_HREF: Final[re.Pattern[str]] = re.compile(r"/postings/\d+", re.IGNORECASE)

#: Header column labels, folded, mapped to the :class:`~models.job.Job` field
#: they fill. Anything not named here is read and discarded: "Division" and
#: "Job Close Date" are real columns with no field to hold them, and inventing
#: one would be worse than dropping them.
_COLUMNS: Final[Dict[str, str]] = {
    "department": "department",
    "position type": "employment_type",
    "employment type": "employment_type",
    "job type": "employment_type",
    "posting number": "job_id",
    "requisition number": "job_id",
    "requisition id": "job_id",
    "job open date": "posted_date",
    "open date": "posted_date",
    "posted date": "posted_date",
    "location": "location",
    "work location": "location",
    "campus": "location",
}


def looks_like_a_board(markup: str) -> bool:
    """Whether markup is a PeopleAdmin search-results page.

    Args:
        markup: The page body.

    Returns:
        ``True`` when the results container is present. A board with no
        openings still has one, which is what separates "no jobs" from "not a
        board" — the two must never be conflated, because one is a fact about
        the institution and the other is a failure to read anything.
    """
    return 'id="search_results"' in markup or "id='search_results'" in markup


def parse_board_url(career_url: str) -> str:
    """Reduce any PeopleAdmin URL to the board's search view.

    Args:
        career_url: A board, search or posting URL.

    Returns:
        The search URL to crawl.

    Raises:
        AdapterUrlError: If the URL has no usable host.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No PeopleAdmin URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        split = urlsplit(raw)
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable PeopleAdmin URL: {career_url!r} ({exc})") from exc

    host = (split.hostname or "").lower()
    if not host or "." not in host:
        raise AdapterUrlError(
            f"{career_url!r} names no host "
            "(expected something like https://jobs.example.edu/postings/search)"
        )

    scheme = split.scheme or "https"
    return f"{scheme}://{split.netloc}{SEARCH_PATH}"


def _column_fields(soup) -> List[str]:
    """Read the header row and say which field each column carries.

    Args:
        soup: The parsed page.

    Returns:
        One entry per column, in order: the :class:`~models.job.Job` field it
        fills, or ``""`` for the title column and for columns with no field.
        Empty when the page has no header, in which case only titles and URLs
        can be trusted.
    """
    header = soup.find(id="job_list_header_responsive")
    if header is None:
        return []

    fields: List[str] = []
    for cell in header.find_all("div"):
        # Only leaf cells carry a label; the wrappers hold other divs.
        if cell.find("div") is not None:
            continue
        label = clean_text(cell).strip().lower()
        fields.append(_COLUMNS.get(label, ""))

    # The first leaf is the title column -- "Job Title" on Montana State, a
    # non-breaking space on Pacific Lutheran. A result row's title lives in an
    # <h3> that :func:`_row_values` skips, so dropping it here is what keeps
    # the two sequences aligned from the spacer cell onwards.
    return fields[1:]


def _row_values(item) -> List[str]:
    """The text of one result row's cells, in order.

    Args:
        item: A ``.job-item`` element.

    Returns:
        One string per cell.
    """
    values: List[str] = []
    for cell in item.find_all("div"):
        if cell.find("div") is not None or cell.find("h3") is not None:
            continue
        values.append(clean_text(cell).strip())
    return values


def _extract_postings(markup: str, page_url: str, company_name: str, board_url: str) -> List[Job]:
    """Parse one results page.

    Args:
        markup: Raw HTML of the results page.
        page_url: URL the markup came from.
        company_name: Company as named in the input sheet.
        board_url: Board URL recorded on each job.

    Returns:
        The postings on this page, deduplicated.
    """
    soup = parse_html(markup)
    fields = _column_fields(soup)

    container = soup.find(id="search_results")
    if container is None:
        return []

    collected: List[Optional[Job]] = []

    for item in container.find_all(class_=re.compile(r"\bjob-item\b")):
        link = item.find("a", href=_POSTING_HREF)
        if link is None:
            continue

        # The anchor text is the title; `data-posting-title` carries the same
        # string and is used only when the anchor is empty.
        title = clean_text(link) or str(item.get("data-posting-title") or "")

        job = build_job(
            company_name=company_name,
            title=title,
            job_url=absolute_url(page_url, str(link.get("href") or "")),
            career_page_url=board_url,
            platform=PLATFORM,
        )
        if job is None:
            continue

        # Match cells to the header by name. The first cell of both is the
        # title, so they stay aligned; a row with fewer cells than the header
        # simply fills fewer fields.
        extra: Dict[str, str] = {}
        for field_name, value in zip(fields, _row_values(item)):
            if field_name and value and not extra.get(field_name):
                extra[field_name] = value

        collected.append(replace(job, **extra) if extra else job)

    return dedupe(collected)


def _next_page(markup: str, page_url: str) -> str:
    """The URL of the next results page, when the board offers one.

    Args:
        markup: The current page.
        page_url: Its URL.

    Returns:
        The next page's URL, or ``""``. Read from the board's own pagination
        links; no page parameter is ever constructed.
    """
    soup = parse_html(markup)
    current = 1
    query = urlsplit(page_url).query
    match = re.search(r"page=(\d+)", query)
    if match:
        current = int(match.group(1))

    for anchor in soup.find_all("a", href=re.compile(r"[?&]page=\d+")):
        href = str(anchor.get("href") or "")
        found = re.search(r"[?&]page=(\d+)", href)
        if found and int(found.group(1)) == current + 1:
            return urljoin(page_url, href)

    return ""


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on a PeopleAdmin board.

    Args:
        career_url: Any PeopleAdmin board, search or posting URL.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting across the board's result pages, deduplicated. Empty when
        the board advertises nothing — which is different from being unable to
        read it, and is not an error.

    Raises:
        AdapterUrlError: If ``career_url`` has no usable host.
        AdapterHttpError: If the board cannot be read, including a page that is
            not a PeopleAdmin results view at all.
    """
    board_url = parse_board_url(career_url)

    logger.info("PeopleAdmin: reading {} for {!r}", board_url, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    collected: List[Optional[Job]] = []
    seen: set = set()

    try:
        url = board_url
        for _page in range(MAX_PAGES):
            markup = get_text(http, url)

            if not looks_like_a_board(markup):
                raise AdapterHttpError(
                    f"{url} is not a PeopleAdmin results page: it carries no "
                    "'search_results' container. The board may have moved, or this "
                    "URL may not be a PeopleAdmin board at all"
                )

            found = _extract_postings(markup, url, company_name, board_url)
            fresh = [job for job in found if job.job_url not in seen]
            if not fresh:
                break

            seen.update(job.job_url for job in fresh)
            collected.extend(fresh)

            url = _next_page(markup, url)
            if not url:
                break
    finally:
        if owned:
            http.close()

    jobs = dedupe(collected)
    logger.success("PeopleAdmin: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
