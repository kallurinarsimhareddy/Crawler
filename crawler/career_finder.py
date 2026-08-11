"""Locate a company's careers page when the input sheet does not give one.

Some rows carry only a marketing website, and some carry a career URL that has
since moved or now redirects to a landing page with no jobs on it. Turning a
company website into a usable jobs board is this module's job.

Two routes, in the order that gets the right answer most often:

1. **Follow the site's own links.** A company's header or footer nearly always
   links to its careers page, and very often links straight out to the
   applicant tracking system that hosts it. A link whose host
   :func:`~crawler.platform_detector.detect_platform` recognises as a real ATS
   is taken immediately and without further checking — it is the best possible
   answer, because it skips the marketing page entirely and lands on the board.
2. **Probe the conventional paths.** ``/careers``, ``/jobs``, ``/join-us`` and
   the rest, tried against the company's own domain when no link was found.

Candidates are scored rather than taken first-come: a footer link reading
"Careers" that points at ``boards.greenhouse.io`` beats one reading "Life at
Acme" that points at a blog post, and the scoring says so explicitly.

Nothing here raises. A site that cannot be read, or that names no careers page,
yields ``""`` and the caller carries on with what the sheet gave it.
"""

from __future__ import annotations

import re
from typing import Final, List, Sequence, Tuple
from urllib.parse import urlsplit

import requests
from loguru import logger

from crawler.platform_detector import Platform, detect_platform
from utils.html import absolute_url, clean_text, parse_html, same_site
from utils.http import AdapterError, get_text

__all__ = ["CAREER_PATHS", "CAREER_WORDS", "find_careers_url", "score_candidate"]

#: Link text and URL fragments that mean "our openings are through here". Every
#: phrasing the brief calls for, plus the ones that show up beside them.
CAREER_WORDS: Final[Tuple[str, ...]] = (
    "careers",
    "career",
    "jobs",
    "job openings",
    "join us",
    "join our team",
    "join the team",
    "work with us",
    "work for us",
    "work here",
    "open positions",
    "open roles",
    "current openings",
    "employment",
    "vacancies",
    "vacancy",
    "opportunities",
    "hiring",
    "we're hiring",
    "recruitment",
    "recruiting",
    "apply",
    "apply now",
    "life at",
    "working at",
    "talent",
    "people",
)

#: Paths worth trying on the company's own domain when its pages link nowhere
#: useful. Ordered by how often each turns out to be the real one.
CAREER_PATHS: Final[Tuple[str, ...]] = (
    "/careers",
    "/careers/",
    "/jobs",
    "/careers/jobs",
    "/company/careers",
    "/about/careers",
    "/about-us/careers",
    "/en/careers",
    "/en-us/careers",
    "/join-us",
    "/work-with-us",
    "/employment",
    "/opportunities",
    "/vacancies",
    "/careers/open-positions",
    "/open-positions",
    "/current-openings",
    "/who-we-are/careers",
)

#: Words in link text that mean the link is about a career elsewhere — a news
#: story, a policy page — rather than this company's own openings.
_NEGATIVE_WORDS: Final[Tuple[str, ...]] = (
    "privacy",
    "cookie",
    "terms",
    "blog",
    "news",
    "press",
    "investor",
    "sitemap",
    "login",
    "sign in",
    "fraud",
    "scam",
    "policy",
    "alumni",
    "internship report",
)

#: A page must hold at least this much evidence of listings to be accepted from
#: a path probe. Counted as occurrences of posting-ish words in the body.
_MIN_EVIDENCE: Final[int] = 2

#: Words whose presence in a page's text suggests it lists openings.
_EVIDENCE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:apply|apply now|open position|current opening|job title|view job|"
    r"full[- ]time|part[- ]time|department|requisition|job id)\b",
    re.IGNORECASE,
)

#: Ceiling on path probes, so discovery cannot become the slowest thing a run
#: does. Each probe is one request.
_MAX_PROBES: Final[int] = 8

#: Score awarded to a link that points straight at a recognised ATS. Chosen to
#: outrank every combination of the text and path signals below, because
#: landing on the board itself is unambiguously the best outcome.
_ATS_SCORE: Final[int] = 100


def _host_of(url: str) -> str:
    """Read a URL's lowercased hostname.

    Args:
        url: Any URL.

    Returns:
        The hostname, or ``""`` if it has none.
    """
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def score_candidate(text: str, url: str, base_url: str) -> int:
    """Rate how likely a link is to lead to this company's job listings.

    Args:
        text: The link's visible text.
        url: The link's absolute URL.
        base_url: The page the link was found on.

    Returns:
        A score; higher is better, and zero or below means "not a candidate".
    """
    if not url:
        return 0

    lowered_text = clean_text(text).lower()
    lowered_url = url.lower()

    if any(word in lowered_text for word in _NEGATIVE_WORDS):
        return 0

    platform = detect_platform(url)
    if platform not in {Platform.GENERIC_HTML, Platform.UNKNOWN}:
        # The link leaves for a real applicant tracking system. Nothing else
        # discovery can find beats that, so short-circuit the rest of the
        # scoring rather than let a weak text signal drag it down.
        return _ATS_SCORE

    score = 0
    if any(word == lowered_text for word in CAREER_WORDS):
        score += 12
    elif any(word in lowered_text for word in CAREER_WORDS):
        score += 7

    if any(f"/{word.replace(' ', '-')}" in lowered_url for word in CAREER_WORDS):
        score += 6
    if re.search(r"/(?:careers?|jobs|employment|vacancies)(?:/|$|\?)", lowered_url):
        score += 8

    # An off-site link that is not a known ATS is usually a social profile.
    if not same_site(base_url, url):
        score -= 5

    return score


def _links_on(markup: str, page_url: str) -> List[Tuple[int, str]]:
    """Score every link on a page as a careers-page candidate.

    Args:
        markup: Raw HTML.
        page_url: URL the markup came from.

    Returns:
        ``(score, url)`` for every scoring link, best first.
    """
    soup = parse_html(markup)
    scored: List[Tuple[int, str]] = []
    seen: set = set()

    for anchor in soup.find_all("a", href=True):
        url = absolute_url(page_url, anchor.get("href"))
        if not url or url in seen:
            continue

        score = score_candidate(clean_text(anchor), url, page_url)
        if score <= 0:
            continue

        seen.add(url)
        scored.append((score, url))

    scored.sort(key=lambda item: -item[0])
    return scored


def _looks_like_listings(markup: str) -> bool:
    """Report whether a page looks like it lists openings.

    Args:
        markup: Raw HTML.

    Returns:
        ``True`` when the page carries enough posting-ish language to be worth
        handing to an adapter.
    """
    if not markup:
        return False
    return len(_EVIDENCE.findall(markup)) >= _MIN_EVIDENCE


def find_careers_url(
    website: str,
    session: requests.Session,
    paths: Sequence[str] = CAREER_PATHS,
    max_probes: int = _MAX_PROBES,
) -> str:
    """Find the careers page for a company, given only its website.

    Args:
        website: The company's marketing website.
        session: Session to use.
        paths: Conventional careers paths to probe when no link is found.
        max_probes: Ceiling on those probes, so discovery stays cheap.

    Returns:
        The best careers or job-board URL found, or ``""`` when the site names
        none. Never raises.
    """
    root = str(website or "").strip()
    if not root:
        return ""
    if "://" not in root:
        root = f"https://{root}"

    if detect_platform(root) is Platform.UNKNOWN:
        logger.debug("Discovery: {!r} is not a usable website", website)
        return ""

    try:
        markup = get_text(session, root)
    except AdapterError as exc:
        logger.debug("Discovery: could not read {} ({})", root, exc)
        markup = ""

    if markup:
        candidates = _links_on(markup, root)
        if candidates:
            score, url = candidates[0]
            if score >= _ATS_SCORE:
                logger.info("Discovery: {} links straight to {}", root, url)
                return url

            # A careers page on the company's own site is worth following one
            # hop, because that is usually where the ATS link actually lives.
            try:
                inner = get_text(session, url)
            except AdapterError:
                inner = ""

            if inner:
                for inner_score, inner_url in _links_on(inner, url):
                    if inner_score >= _ATS_SCORE:
                        logger.info("Discovery: {} leads on to {}", url, inner_url)
                        return inner_url

            logger.info("Discovery: using {} for {}", url, root)
            return url

    origin = f"{urlsplit(root).scheme or 'https'}://{_host_of(root)}"
    for path in list(paths)[:max_probes]:
        probe = f"{origin}{path}"
        try:
            body = get_text(session, probe, allow_statuses=(404, 403, 410))
        except AdapterError:
            continue

        if _looks_like_listings(body):
            logger.info("Discovery: found {} by probing", probe)
            return probe

    logger.debug("Discovery: no careers page found for {}", root)
    return ""
