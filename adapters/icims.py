"""Extract job postings from iCIMS career portals.

iCIMS portals are served from ``<tenant>.icims.com``, frequently iframed into
the company's own careers page, and they page server-side through a search
results view::

    https://<tenant>.icims.com/jobs/search?ss=1&in_iframe=1&pr=<page>

There is no public JSON API, so the rendered result rows are parsed. Rows carry
the posting link and, in most themes, a location line beside it; anything the
iCIMS-specific selectors miss falls through to the generic job-card extraction.

**Many tenants sit behind an AWS WAF interstitial.** It is not a wall: its
script computes a token, sets a cookie and expects the visitor to come back.
Plain HTTP can never run that script, so those portals answer every request
with the challenge instead of the board. :func:`fetch_jobs` therefore tries
HTTP first — unchanged, and the only path a readable tenant ever takes — and
falls back to :func:`utils.browser.render`, which already knows how to wait a
challenge out and reload past it, only when the challenge is what came back.

The fallback is bounded by :data:`MAX_RENDER_PAGES`, and it refuses to guess: a
portal the browser cannot read either is reported as blocked with the original
message rather than as a company with no jobs.
"""

from __future__ import annotations

import re
from typing import Final, List, Optional
from urllib.parse import urlsplit

import requests
from loguru import logger

from adapters._paginated_html import crawl_pages
from adapters.generic import extract_jobs
from models.job import Job
from utils.browser import render as render_page
from utils.html import absolute_url, clean_text, parse_html
from utils.http import AdapterHttpError, AdapterUrlError, build_session
from utils.jobs import build_job, dedupe

__all__ = [
    "MAX_RENDER_PAGES",
    "PLATFORM",
    "WafChallenge",
    "fetch_jobs",
    "looks_like_a_challenge",
    "parse_portal_host",
]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "iCIMS"

#: Result pages to walk before giving up.
_MAX_PAGES: Final[int] = 60

#: Links to an individual posting: /jobs/<id>/<slug>/job
_JOB_HREF: Final[re.Pattern[str]] = re.compile(r"/jobs/\d+/", re.IGNORECASE)

#: Class names iCIMS themes use for the location line of a result row.
_LOCATION_CLASS: Final[re.Pattern[str]] = re.compile(
    r"iCIMS_JobHeaderTag|JobLocation|job-location|location", re.IGNORECASE
)

#: Markers of the AWS WAF bot challenge iCIMS serves in place of the board.
_WAF_MARKERS: Final[tuple] = ("awsWafCookieDomainList", "Human Verification", "gokuProps")

#: Result pages the browser may visit for one company. A render costs seconds
#: where a fetch costs milliseconds, so a blocked tenant is read to a bounded
#: depth rather than exhaustively; ``_MAX_PAGES`` still governs the HTTP path.
MAX_RENDER_PAGES: Final[int] = 12


class WafChallenge(AdapterHttpError):
    """The portal served its bot challenge instead of the board.

    A subclass so every existing caller that handles :class:`AdapterHttpError`
    is unaffected, while :func:`fetch_jobs` can tell this apart from a portal
    that is genuinely broken and decide to open a browser.
    """


def looks_like_a_challenge(markup: str) -> bool:
    """Whether markup is the WAF interstitial rather than the job board.

    Args:
        markup: The page body.

    Returns:
        ``True`` when the page is a challenge. A board with no postings on it
        is **not** a challenge — zero jobs is a fact about the company, and
        conflating the two would report every quiet board as blocked.
    """
    return any(marker in markup for marker in _WAF_MARKERS)


def parse_portal_host(career_url: str) -> str:
    """Read the iCIMS portal hostname out of a URL.

    Args:
        career_url: An iCIMS portal, search or posting URL.

    Returns:
        The portal hostname.

    Raises:
        AdapterUrlError: If the URL is not an iCIMS portal.
    """
    raw = str(career_url or "").strip()
    if not raw:
        raise AdapterUrlError("No iCIMS URL supplied")
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError as exc:
        raise AdapterUrlError(f"Unparsable iCIMS URL: {career_url!r} ({exc})") from exc

    if not host.endswith("icims.com"):
        raise AdapterUrlError(
            f"{career_url!r} is not an iCIMS portal URL "
            "(expected something like https://careers-acme.icims.com/jobs/search)"
        )

    return host


def _extract_rows(markup: str, page_url: str, company_name: str, board_url: str) -> List[Job]:
    """Parse one iCIMS search results page.

    Args:
        markup: Raw HTML of the results page.
        page_url: URL the markup came from.
        company_name: Company as named in the input sheet.
        board_url: Portal URL recorded on each job.

    Returns:
        The postings on this page, deduplicated. Falls back to generic
        extraction when the iCIMS selectors match nothing, so an unfamiliar
        theme still yields results.
    """
    if looks_like_a_challenge(markup):
        raise WafChallenge(
            f"{page_url} served an AWS WAF bot challenge instead of the job board. iCIMS fronts "
            "its portals with a human-verification interstitial that cannot be satisfied over "
            "plain HTTP; reading this tenant needs the browser-driven path"
        )

    soup = parse_html(markup)
    collected: List[Optional[Job]] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if not _JOB_HREF.search(href):
            continue

        title = clean_text(anchor)
        if not title:
            continue

        # The location sits in a tagged element within the same result row.
        location = ""
        row = anchor.find_parent(attrs={"class": re.compile(r"row|job", re.IGNORECASE)}) or anchor.parent
        if row is not None:
            tag = row.find(attrs={"class": _LOCATION_CLASS})
            if tag is not None:
                location = clean_text(tag)

        collected.append(
            build_job(
                company_name=company_name,
                title=title,
                job_url=absolute_url(page_url, href),
                location=location,
                career_page_url=board_url,
                platform=PLATFORM,
            )
        )

    jobs = dedupe(collected)
    if jobs:
        return jobs

    return extract_jobs(markup, page_url, company_name, PLATFORM, career_page_url=board_url)


def _read_in_browser(host: str, company_name: str, board_url: str):
    """Walk the portal's result pages in a real browser.

    Used only after HTTP has come back with the challenge.
    :func:`utils.browser.render` clears the interstitial itself — waiting for
    its script to compute a token and reloading past it — so the work here is
    only to page through the results and hand each rendered document to the
    same extractor the HTTP path uses. Deduplication and job identity are
    therefore identical whichever path produced the markup.

    Args:
        host: The portal hostname.
        company_name: Company as named in the input sheet.
        board_url: Portal URL recorded on each job.

    Returns:
        ``(jobs, cleared)``. ``cleared`` says whether the browser actually got
        past the challenge and saw a board, which is the difference between a
        company with no openings and a portal that is still blocked. Both
        return no jobs, and only one of them is an error.
    """
    collected: List[Optional[Job]] = []
    seen: set = set()
    cleared = False

    for page in range(MAX_RENDER_PAGES):
        url = f"https://{host}/jobs/search?ss=1&in_iframe=1&pr={page}"

        try:
            rendered = render_page(url, capture_network=True)
        except Exception:  # noqa: BLE001 - a missing browser is not a crash
            logger.opt(exception=True).debug("iCIMS: render failed for {}", url)
            return dedupe(collected), cleared

        if rendered is None or not getattr(rendered, "ok", False):
            logger.debug("iCIMS: the browser returned nothing for {}", url)
            return dedupe(collected), cleared

        markup = rendered.html or ""
        if looks_like_a_challenge(markup):
            # The browser could not clear it either — a CAPTCHA, most likely.
            # Reporting that honestly is the caller's job, not this one's.
            logger.info("iCIMS: {} is still challenged after rendering", url)
            return dedupe(collected), cleared

        # Past the interstitial: whatever this page holds, it is the board.
        cleared = True

        # The URL the browser actually landed on, so a redirect cannot strand
        # every posting link against the wrong base.
        found = _extract_rows(markup, rendered.url or url, company_name, board_url)

        fresh = [job for job in found if job.job_url not in seen]
        if not fresh:
            break

        seen.update(job.job_url for job in fresh)
        collected.extend(fresh)

    return dedupe(collected), cleared


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
) -> List[Job]:
    """Fetch every posting on an iCIMS portal.

    Args:
        career_url: Any iCIMS portal, search or posting URL for the company.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.

    Returns:
        Every posting found across the portal's result pages, deduplicated.

    Raises:
        AdapterUrlError: If ``career_url`` is not an iCIMS URL.
        AdapterHttpError: If the portal cannot be read at all — including a
            tenant whose bot challenge survives the browser, which is reported
            as blocked rather than as a company with no openings.
    """
    host = parse_portal_host(career_url)
    board_url = f"https://{host}/jobs/search?ss=1"

    logger.info("iCIMS: portal {} for {!r}", host, company_name)

    owned = session is None
    http = session if session is not None else build_session()

    try:
        jobs = crawl_pages(
            http,
            lambda page: f"https://{host}/jobs/search?ss=1&in_iframe=1&pr={page}",
            company_name,
            PLATFORM,
            board_url,
            max_pages=_MAX_PAGES,
            extract=lambda markup, url: _extract_rows(markup, url, company_name, board_url),
            # iCIMS answers the challenge with 403/405; the body is what
            # identifies it, so it must reach the extractor to be reported.
            allow_statuses=(403, 405),
        )
    except WafChallenge as challenge:
        # HTTP cannot run the challenge script. A browser can, so try once --
        # and if it comes back with nothing, report the original blocker
        # rather than inventing a board or claiming the company has no jobs.
        logger.info("iCIMS: {!r} is behind a bot challenge, trying the browser", company_name)
        jobs, cleared = _read_in_browser(host, company_name, board_url)

        if not cleared:
            # The browser never saw the board. Reporting the original blocker
            # is the honest outcome; claiming no openings would be a guess.
            raise challenge

        logger.success(
            "iCIMS: recovered {} job(s) for {!r} in the browser", len(jobs), company_name
        )
        return jobs
    finally:
        if owned:
            http.close()

    logger.success("iCIMS: {} job(s) for {!r}", len(jobs), company_name)
    return jobs
