"""Shared paging and extraction loop for the adapters that read HTML listings.

Most of the applicant tracking systems in the long tail publish their boards as
server-rendered HTML. They differ in only three ways: how a page number becomes
a URL, which links are postings, and whether the pager is a query parameter or
a "next" link. Everything else — fetch, extract, deduplicate, stop when a page
adds nothing new — is identical, so it lives here once.

Two entry points, in increasing order of what they do for you:

* :func:`crawl_pages` walks a listing paged by a query parameter. This is the
  original helper and its behaviour is unchanged.
* :func:`crawl_board` is the version 2 workhorse. It adds vendor-specific link
  patterns, "next link" pagination, embedded-JavaScript extraction and the
  headless-browser retry, so a new adapter for an HTML board is usually a URL
  parser plus one call.

This is a private helper for :mod:`adapters`; it registers no platform of its
own and the engine never looks it up.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import requests
from loguru import logger

from adapters.generic import (
    card_of,
    extract_jobs,
    location_in,
    looks_like_title,
    render_and_extract,
)
from config.settings import SETTINGS
from models.job import Job
from utils.discovery import jobs_from_state
from utils.html import absolute_url, clean_text, parse_html
from utils.http import AdapterHttpError, AdapterUrlError, build_session, get_text
from utils.jobs import build_job, dedupe

__all__ = [
    "crawl_board",
    "crawl_pages",
    "extract_with_pattern",
    "fetch_hosted_board",
    "normalise_board_url",
]

#: Signature of a per-page extractor: markup and its URL in, jobs out.
PageExtractor = Callable[[str, str], List[Job]]

#: Ceiling on pages walked by :func:`crawl_board` when nothing else stops it.
DEFAULT_MAX_PAGES: int = 25

#: Link text that marks the pager's forward control.
_NEXT_TEXT: frozenset = frozenset(
    {"next", "next page", "next »", "next >", "›", "»", ">", "load more", "show more", "more"}
)


def crawl_pages(
    session: requests.Session,
    url_for_page: Callable[[int], str],
    company_name: str,
    platform: str,
    board_url: str,
    max_pages: int = 50,
    first_page: int = 0,
    extract: Optional[PageExtractor] = None,
    allow_statuses: tuple = (),
) -> List[Job]:
    """Walk a paged HTML listing until it stops yielding new postings.

    Args:
        session: Session to use.
        url_for_page: Turns a zero-based page index into the URL to fetch.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        board_url: Board URL recorded on each job.
        max_pages: Hard stop, so a board that pages forever cannot hang a run.
        first_page: Index passed to ``url_for_page`` for the first request.
        extract: Platform-specific extractor. Defaults to the generic
            structured-data and job-card extraction, which handles most boards.
        allow_statuses: Non-2xx statuses whose body should still be handed to
            ``extract``, so a platform serving an interstitial can recognise it
            and report precisely what happened.

    Returns:
        Every posting found across the pages, deduplicated.

    Raises:
        AdapterHttpError: If the *first* page cannot be fetched. Later pages
            failing is treated as the end of the listing, since many boards
            answer with an error once the pages run out.
    """
    collected: List[Job] = []

    for offset in range(max_pages):
        index = first_page + offset
        page_url = url_for_page(index)

        try:
            markup = get_text(session, page_url, allow_statuses=allow_statuses)
        except AdapterHttpError:
            if offset == 0:
                raise
            logger.debug("{}: page {} unavailable, treating as the end", platform, index)
            break

        found = (
            extract(markup, page_url)
            if extract is not None
            else extract_jobs(markup, page_url, company_name, platform, career_page_url=board_url)
        )

        before = len(collected)
        collected = dedupe(collected + found)
        added = len(collected) - before

        logger.debug("{}: page {} gave {} posting(s), {} new", platform, index, len(found), added)

        if added == 0:
            break

    return collected


def normalise_board_url(career_url: str, platform: str, hosts: Sequence[str] = ()) -> str:
    """Validate a hosted board's URL and return it in a fetchable shape.

    Every hosted-board adapter needs the same three checks — that there is a
    URL, that it parses, and that it belongs to this vendor — so they are done
    once here rather than in each adapter.

    Args:
        career_url: The URL from the input sheet.
        platform: Label for the ``Platform`` column, used in error messages.
        hosts: Registrable domains this vendor serves boards from. A URL on any
            other host is rejected. Empty means accept any host, which is right
            for the vendors that host boards on the customer's own domain.

    Returns:
        The URL with a scheme, ready to fetch.

    Raises:
        AdapterUrlError: If the URL is blank, unparsable, or not this vendor's.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError(f"No {platform} URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable {platform} URL: {career_url!r} ({exc})") from exc

    if not host:
        raise AdapterUrlError(f"No host in {platform} URL {career_url!r}")

    if hosts and not any(host == domain or host.endswith(f".{domain}") for domain in hosts):
        raise AdapterUrlError(
            f"{career_url!r} is not a {platform} board (expected a host under {', '.join(hosts)})"
        )

    return raw


def fetch_hosted_board(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session],
    platform: str,
    *,
    hosts: Sequence[str] = (),
    job_url_pattern: Optional[re.Pattern[str]] = None,
    url_for_page: Optional[Callable[[int], str]] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    first_page: int = 1,
    allow_statuses: Tuple[int, ...] = (),
    headers: Optional[Dict[str, str]] = None,
    follow_next: bool = True,
    board_url: Optional[str] = None,
) -> List[Job]:
    """Run a complete hosted-board crawl, including session ownership.

    This is the whole body of a server-rendered board adapter. An adapter
    supplies only what is specific to its vendor — the hosts it serves from,
    the shape of its posting URLs, and how it pages — and this does the rest,
    so the twenty-odd HTML-board adapters share one implementation instead of
    twenty near-identical ones.

    Args:
        career_url: The URL from the input sheet.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when ``None``.
        platform: Label for the ``Platform`` column.
        hosts: Registrable domains this vendor serves boards from.
        job_url_pattern: Matches this vendor's posting URLs.
        url_for_page: Turns a page index into a URL, for query-paged boards.
        max_pages: Hard stop on pages walked.
        first_page: Index passed to ``url_for_page`` for the first request.
        allow_statuses: Non-2xx statuses whose body should still be parsed.
        headers: Extra headers for every request.
        follow_next: Whether to follow "next" links.
        board_url: Board URL recorded on each job. Defaults to ``career_url``
            once normalised.

    Returns:
        Every posting found, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` is not usable for this vendor.
        AdapterHttpError: If the board cannot be read at all.
    """
    url = normalise_board_url(career_url, platform, hosts)
    board = board_url or url

    logger.info("{}: reading {} for {!r}", platform, board, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        jobs = crawl_board(
            http,
            board,
            company_name,
            platform,
            job_url_pattern=job_url_pattern,
            url_for_page=url_for_page,
            max_pages=max_pages,
            first_page=first_page,
            allow_statuses=allow_statuses,
            headers=headers,
            follow_next=follow_next,
        )
    finally:
        if owned:
            http.close()

    logger.success("{}: {} job(s) for {!r}", platform, len(jobs), company_name)
    return jobs


def _without_fragment(url: str) -> str:
    """Strip a URL's fragment, so ``#top`` does not make it a different page.

    Args:
        url: Any URL.

    Returns:
        The URL up to but not including ``#``, with any trailing slash removed
        so ``/board`` and ``/board/`` compare equal.
    """
    return str(url or "").split("#", 1)[0].rstrip("/")


def extract_with_pattern(
    markup: str,
    page_url: str,
    company_name: str,
    platform: str,
    career_page_url: str,
    job_url_pattern: re.Pattern[str],
) -> List[Job]:
    """Extract postings by matching the vendor's own job-URL shape.

    A hosted board is easier to read than an unknown page: the vendor's posting
    URLs follow one exact layout, so the guesswork the generic extractor has to
    do about which links are jobs simply does not apply. Everything after
    "which links are jobs" — finding the card, reading the title, locating the
    posting — is the generic reasoning, reused rather than reimplemented.

    Args:
        markup: Raw HTML.
        page_url: URL the markup came from.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        career_page_url: Board URL recorded on each job.
        job_url_pattern: Matches the vendor's posting URLs and nothing else.

    Returns:
        The postings found, deduplicated. Empty when no link matched.
    """
    soup = parse_html(markup)
    collected: List[Optional[Job]] = []

    # A link back to the page being read is never a posting. Boards link to
    # themselves constantly — language switchers, pagers, "clear filters" —
    # and a pattern loose enough to match the board's own path would otherwise
    # report every one of those as a job.
    self_links = {_without_fragment(page_url), _without_fragment(career_page_url)}

    for anchor in soup.find_all("a", href=True):
        url = absolute_url(page_url, anchor.get("href"))
        if not url or not job_url_pattern.search(url):
            continue
        if _without_fragment(url) in self_links:
            continue

        card = card_of(anchor)
        title = clean_text(anchor)

        if not looks_like_title(title):
            # The link is an "Apply" button or an image; the title is nearby.
            for node in card.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong"]):
                candidate = clean_text(node)
                if looks_like_title(candidate):
                    title = candidate
                    break

        if not looks_like_title(title):
            continue

        collected.append(
            build_job(
                company_name=company_name,
                title=title,
                job_url=url,
                location=location_in(card, title),
                career_page_url=career_page_url or page_url,
                platform=platform,
            )
        )

    return dedupe(collected)


def _extract_page(
    markup: str,
    page_url: str,
    company_name: str,
    platform: str,
    career_page_url: str,
    job_url_pattern: Optional[re.Pattern[str]],
) -> List[Job]:
    """Run every extraction route over one page, best first.

    Args:
        markup: Raw HTML.
        page_url: URL the markup came from.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        career_page_url: Board URL recorded on each job.
        job_url_pattern: The vendor's posting-URL shape, when it has one.

    Returns:
        The postings found on this page.
    """
    if job_url_pattern is not None:
        jobs = extract_with_pattern(
            markup, page_url, company_name, platform, career_page_url, job_url_pattern
        )
        if jobs:
            return jobs

    jobs = extract_jobs(markup, page_url, company_name, platform, career_page_url=career_page_url)
    if jobs:
        return jobs

    return jobs_from_state(
        markup, page_url, company_name, platform, career_page_url=career_page_url
    )


def _next_link(markup: str, page_url: str, seen: Sequence[str]) -> str:
    """Find the pager's forward link, if the board uses one.

    Args:
        markup: Raw HTML.
        page_url: URL the markup came from.
        seen: URLs already visited, so a cyclic pager terminates.

    Returns:
        The next page's URL, or ``""``.
    """
    soup = parse_html(markup)

    candidates = list(soup.find_all("a", attrs={"rel": "next"}, href=True))
    candidates += [
        anchor
        for anchor in soup.find_all("a", href=True)
        if clean_text(anchor).lower().strip(" .") in _NEXT_TEXT
    ]

    for anchor in candidates:
        url = absolute_url(page_url, anchor.get("href"))
        if url and url not in seen:
            return url

    return ""


def crawl_board(
    session: requests.Session,
    board_url: str,
    company_name: str,
    platform: str,
    *,
    job_url_pattern: Optional[re.Pattern[str]] = None,
    url_for_page: Optional[Callable[[int], str]] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    first_page: int = 1,
    allow_statuses: Tuple[int, ...] = (),
    headers: Optional[Dict[str, str]] = None,
    follow_next: bool = True,
    use_browser: Optional[bool] = None,
) -> List[Job]:
    """Read every posting from a server-rendered board.

    Pages are walked either by building each page's URL from its number
    (``url_for_page``) or by following the pager's "next" link, and the walk
    stops as soon as a page contributes nothing new. If the whole HTTP pass
    finds nothing — the board renders client-side, or sits behind an
    interstitial — the board URL is retried once in headless Chromium, which
    also handles *Load more* buttons and infinite scroll.

    Args:
        session: Session to use.
        board_url: The board's listing URL.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        job_url_pattern: Matches this vendor's posting URLs. Supplying one
            makes extraction exact rather than heuristic.
        url_for_page: Turns a page index into a URL. When omitted, only
            ``board_url`` is fetched and pagination follows "next" links.
        max_pages: Hard stop, so a board that pages forever cannot hang a run.
        first_page: Index passed to ``url_for_page`` for the first request.
        allow_statuses: Non-2xx statuses whose body should still be parsed.
        headers: Extra headers for every request.
        follow_next: Whether to follow "next" links. Ignored when
            ``url_for_page`` is supplied, since that already drives paging.
        use_browser: Whether to allow the browser retry. Defaults to the run's
            :data:`~config.settings.SETTINGS` value.

    Returns:
        Every posting found, deduplicated. Empty when the board advertises
        none.

    Raises:
        AdapterHttpError: If the *first* page cannot be fetched and the browser
            retry did not rescue it. Later pages failing is treated as the end
            of the listing, since many boards error once the pages run out.
    """
    browser_allowed = SETTINGS.browser_fallback if use_browser is None else use_browser

    collected: List[Job] = []
    visited: List[str] = []
    transport_error: Optional[AdapterHttpError] = None
    current = board_url

    for index in range(max_pages):
        page_url = url_for_page(first_page + index) if url_for_page is not None else current
        if not page_url or page_url in visited:
            break

        try:
            markup = get_text(session, page_url, headers=headers, allow_statuses=allow_statuses)
        except AdapterHttpError as exc:
            if index == 0:
                transport_error = exc
            else:
                logger.debug("{}: page {} unavailable, treating as the end", platform, index)
            break

        visited.append(page_url)

        found = _extract_page(
            markup, page_url, company_name, platform, board_url, job_url_pattern
        )
        before = len(collected)
        collected = dedupe(collected + found)
        added = len(collected) - before

        logger.debug(
            "{}: page {} gave {} posting(s), {} new", platform, index + first_page, len(found), added
        )

        if added == 0:
            break

        if url_for_page is None:
            if not follow_next:
                break
            current = _next_link(markup, page_url, visited)
            if not current:
                break

    if not collected and browser_allowed:
        rendered = render_and_extract(board_url, company_name, platform, career_page_url=board_url)
        if rendered:
            logger.success(
                "{}: {} job(s) for {!r} via the browser", platform, len(rendered), company_name
            )
            return rendered

    if not collected and transport_error is not None:
        raise transport_error

    return collected
