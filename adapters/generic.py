"""Best-effort extraction for career pages with no recognised platform.

This is the fallback used whenever :mod:`crawler.platform_detector` cannot name
an ATS — bespoke careers pages, small in-house boards, and long-tail systems
that do not warrant a dedicated adapter. It trades precision for coverage.

Five strategies are tried in order of how much the page tells us, and the first
one that yields postings wins:

1. **JSON-LD** ``JobPosting`` objects. Publishers embed these for search
   engines, and they already carry title, location and URL.
2. **Microdata** ``JobPosting`` annotations, the older equivalent.
3. **Repeating job cards.** Every link that looks like a posting is grouped by
   its position in the DOM; the largest group of structurally identical links is
   the job list, whether the page renders it as a table, a card grid or a plain
   list. Titles come from the link, locations from the surrounding card.
4. **Apply links.** Boards that render the title as a heading and link only an
   "Apply" button defeat strategy 3, because the link text is furniture. Here
   the link is kept and the title is read from the card's heading instead,
   which is what accordions and most table layouts look like.
5. **Embedded JavaScript state.** ``__NEXT_DATA__``, ``__NUXT__``, Apollo
   caches and hand-rolled ``window.*`` payloads, via :mod:`utils.discovery`.
   This is what reads a page whose DOM holds no listings at all because the
   framework renders them client-side.

If every strategy comes up empty and the run allows it, the page is loaded
again in headless Chromium — which also clicks *Load more*, scrolls an infinite
list to its end, and captures the board's own XHR responses.

Extraction never raises. A page that yields nothing produces an empty list and
the caller moves on. Transport failures do still surface, so a dead link is
distinguishable from an empty board.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Final, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup, Tag
from loguru import logger

from config.settings import SETTINGS
from models.job import Job
from utils.discovery import jobs_from_payload, jobs_from_state
from utils.html import (
    absolute_url,
    clean_text,
    job_postings_from_microdata,
    json_ld_job_postings,
    parse_html,
)
from utils.http import build_session, get_text
from utils.jobs import build_job, dedupe

__all__ = [
    "PLATFORM",
    "card_of",
    "extract_jobs",
    "fetch_jobs",
    "jobs_from_rendered_page",
    "location_in",
    "looks_like_job_url",
    "looks_like_title",
    "render_and_extract",
]

#: Label written to the ``Platform`` column.
PLATFORM: Final[str] = "Generic HTML"

#: URL fragments that mark a link as a job posting rather than site furniture.
_JOB_URL_HINTS: Final[Tuple[str, ...]] = (
    "/job/",
    "/jobs/",
    "/job-",
    "/jobs-",
    "jobid=",
    "job_id=",
    "jobcode=",
    "/careers/",
    "/career/",
    "/opening",
    "/openings/",
    "/position",
    "/positions/",
    "/vacanc",
    "/requisition",
    "/opportunit",
    "/apply/",
    "/joblisting",
    "/job-detail",
    "/jobdetail",
    "/jobposting",
    "/viewjob",
)

#: Link text that is navigation, not a job title.
_NAV_TEXT: Final[frozenset] = frozenset(
    {
        "apply",
        "apply now",
        "apply here",
        "view",
        "view job",
        "view all",
        "view all jobs",
        "view details",
        "see all",
        "see all jobs",
        "see more",
        "learn more",
        "read more",
        "more",
        "more info",
        "details",
        "job details",
        "search",
        "search jobs",
        "all jobs",
        "open positions",
        "current openings",
        "careers",
        "career",
        "jobs",
        "next",
        "previous",
        "prev",
        "back",
        "home",
        "submit",
        "sign in",
        "log in",
        "login",
        "register",
        "share",
        "email",
        "print",
        "save",
        "save job",
        "join our team",
        "join us",
        "click here",
        "locations",
        "students",
        "graduates",
        "diversity & inclusion",
        "our team",
        "meet our team",
        "life at",
    }
)

#: Link text that is an action, not a posting title. Boards label their links
#: "Apply online", "Apply for this position", "Apply →" and "View & Apply", and
#: an exact-match list never keeps up with the variations. Matching the shape
#: instead is what lets :func:`_jobs_from_apply_links` recognise the link as a
#: button and go looking for the real title in the card around it.
#:
#: The word boundary matters: "Applications Engineer" is a job, "Apply now" is
#: not, and only ``\bapply\b`` tells them apart.
_NAV_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:apply\b"
    r"|(?:view|see|read|learn|find|show)\b[^|]{0,20}?\b(?:apply|more|details?|jobs?)\b"
    r"|more\s+(?:info|details)"
    r"|full\s+(?:details|description))",
    re.IGNORECASE,
)

#: Attribute values that mark an element as carrying a location.
_LOCATION_HINT: Final[re.Pattern[str]] = re.compile(
    r"location|city|region|address|office|where|geo|place", re.IGNORECASE
)

#: Attribute values that mark an element as carrying the posting's title.
_TITLE_HINT: Final[re.Pattern[str]] = re.compile(
    r"job[-_]?title|position[-_]?title|posting[-_]?title|role[-_]?title|\btitle\b|\bheading\b",
    re.IGNORECASE,
)

#: "Austin, TX" / "London, United Kingdom" appearing in a card's free text.
_LOCATION_TEXT: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Z][A-Za-z.'\-]+(?:[ ][A-Z][A-Za-z.'\-]+){0,3},\s*(?:[A-Z]{2}\b|[A-Z][a-z]+(?:[ ][A-Z][a-z]+){0,2}))"
)

#: Bounds on a plausible job title, in characters.
_MIN_TITLE: Final[int] = 3
_MAX_TITLE: Final[int] = 160

#: A repeating group must have at least this many members to be a job list.
_MIN_GROUP: Final[int] = 3

#: How far up the DOM a link's structural signature is taken.
_SIGNATURE_DEPTH: Final[int] = 3

#: How far :func:`_card_of` will climb looking for a posting's card. Generous,
#: because the climb is bounded by content rather than by depth — modern boards
#: wrap a single card in several layout elements.
_MAX_CARD_DEPTH: Final[int] = 8

#: Posting links a single card may contain. Two, because a card commonly links
#: the title and an "Apply" button at the same posting.
_MAX_LINKS_PER_CARD: Final[int] = 2

#: Pages followed when a listing paginates. Generic pages are shallow; this is a
#: safety bound, not a quality target.
_MAX_PAGES: Final[int] = 10


def _looks_like_job_url(url: str) -> bool:
    """Report whether a URL looks like an individual posting.

    Args:
        url: An absolute URL.

    Returns:
        ``True`` if the path carries any known posting marker.
    """
    lowered = url.lower()
    return any(hint in lowered for hint in _JOB_URL_HINTS)


def _looks_like_title(text: str) -> bool:
    """Report whether link text could be a job title.

    Args:
        text: The link's visible text.

    Returns:
        ``True`` if it is the right length and is not navigation furniture.
    """
    if not (_MIN_TITLE <= len(text) <= _MAX_TITLE):
        return False
    if text.lower().strip(" .:*>|-") in _NAV_TEXT:
        return False
    if _NAV_PATTERN.match(text):
        return False
    # Pure punctuation or digits is never a title.
    return any(character.isalpha() for character in text)


def _signature(anchor: Tag) -> Tuple[Any, ...]:
    """Describe where a link sits in the DOM, for grouping.

    Two links with the same signature occupy the same slot in two repetitions of
    the same template — which is exactly what a job list is.

    Args:
        anchor: The link.

    Returns:
        A hashable signature of the link and its nearest ancestors.
    """
    signature: List[Tuple[str, Tuple[str, ...]]] = []
    node: Optional[Tag] = anchor

    for _ in range(_SIGNATURE_DEPTH):
        if node is None or not isinstance(node, Tag):
            break
        classes = node.get("class") or []
        signature.append((node.name, tuple(sorted(str(value) for value in classes))))
        node = node.parent

    return tuple(signature)


def _card_of(anchor: Tag) -> Tag:
    """Find the element that represents one posting around a link.

    A card holds exactly one posting, and that is what bounds the climb: as
    soon as the next ancestor would take in a second posting link, the current
    node is the card. Climbing a fixed number of levels instead — which is what
    version 1 did — swallowed the whole listing on any board that nests its
    cards more deeply than four elements, and the title then came back as
    whatever heading sat at the top of the *list* ("R&D", "Open Positions")
    rather than the job.

    Args:
        anchor: The link.

    Returns:
        The nearest ancestor that represents a single posting, falling back to
        the link itself when even its parent holds several.
    """
    best: Tag = anchor
    node: Optional[Tag] = anchor.parent

    for _ in range(_MAX_CARD_DEPTH):
        if node is None or not isinstance(node, Tag):
            break

        # Two links are normal within one card — the title and an "Apply"
        # button usually both point at the posting. More than that means this
        # node spans several postings, so the card is the one below it.
        if best is not anchor and _posting_links_in(node) > _MAX_LINKS_PER_CARD:
            break

        best = node
        if node.name in {"li", "tr", "article"}:
            return node
        node = node.parent

    return best


def _posting_links_in(node: Tag) -> int:
    """Count the links inside a node that look like job postings.

    Args:
        node: Any element.

    Returns:
        How many of its links point at something posting-shaped.
    """
    return sum(
        1
        for link in node.find_all("a", href=True)
        if _looks_like_job_url(str(link.get("href") or ""))
    )


def _location_in(card: Tag, title: str) -> str:
    """Find the location published alongside a posting.

    Args:
        card: The element representing one posting.
        title: The posting title, so it is not mistaken for a location.

    Returns:
        The location, or ``""`` when the card names none.
    """
    for attribute in ("itemprop", "class", "id", "data-testid"):
        for node in card.find_all(attrs={attribute: _LOCATION_HINT}):
            text = clean_text(node)
            if text and text != title and len(text) <= 120:
                return text

    text = clean_text(card)
    if title:
        text = text.replace(title, " ")

    match = _LOCATION_TEXT.search(text)
    return match.group(1).strip() if match else ""


#: Public names for the three helpers the platform adapters reuse. A hosted
#: board differs from an unknown page only in knowing which links are postings;
#: the "what is this card" and "where is the location" reasoning is identical,
#: so it is shared rather than reimplemented per vendor.
card_of = _card_of
location_in = _location_in
looks_like_title = _looks_like_title
looks_like_job_url = _looks_like_job_url


def _title_near(anchor: Tag, card: Tag) -> str:
    """Find the posting title for a link whose own text is furniture.

    Args:
        anchor: The link, typically an "Apply" button.
        card: The element representing one posting.

    Returns:
        The heading text that titles this posting, or ``""``.
    """
    for node in card.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"]):
        text = clean_text(node)
        if _looks_like_title(text):
            return text

    for attribute in ("itemprop", "class", "id", "data-testid"):
        for node in card.find_all(attrs={attribute: _TITLE_HINT}):
            text = clean_text(node)
            if _looks_like_title(text):
                return text

    # An accordion puts the heading just before the panel holding the link.
    previous = card.find_previous(["h1", "h2", "h3", "h4", "h5", "h6", "summary", "button"])
    if previous is not None:
        text = clean_text(previous)
        if _looks_like_title(text):
            return text

    # A table row names the posting in its first cell.
    row = anchor.find_parent("tr")
    if row is not None:
        cells = row.find_all(["td", "th"])
        if cells:
            text = clean_text(cells[0])
            if _looks_like_title(text):
                return text

    return ""


def _jobs_from_apply_links(
    soup: BeautifulSoup, page_url: str, company_name: str, platform: str, career_page_url: str
) -> List[Optional[Job]]:
    """Build jobs from links whose text is "Apply" rather than the title.

    Card grids, accordions and tables routinely render the title as a heading
    and give the link a generic label. The card-grouping strategy discards
    those links because the text is navigation furniture, so this strategy
    keeps the link and looks for the title around it instead.

    Args:
        soup: The parsed page.
        page_url: URL the page came from.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        career_page_url: Board URL recorded on each job.

    Returns:
        One entry per posting; unusable postings are ``None``.
    """
    collected: List[Optional[Job]] = []
    seen_urls: set = set()

    for anchor in soup.find_all("a", href=True):
        url = absolute_url(page_url, anchor.get("href"))
        if not url or url in seen_urls or not _looks_like_job_url(url):
            continue

        text = clean_text(anchor)
        if _looks_like_title(text):
            # The card strategy already covers this shape and does it better.
            continue

        card = _card_of(anchor)
        title = _title_near(anchor, card)
        if not title:
            continue

        seen_urls.add(url)
        collected.append(
            build_job(
                company_name=company_name,
                title=title,
                job_url=url,
                location=_location_in(card, title),
                career_page_url=career_page_url or page_url,
                platform=platform,
            )
        )

    return collected


def _jobs_from_json_ld(
    soup: BeautifulSoup, page_url: str, company_name: str, platform: str, career_page_url: str
) -> List[Optional[Job]]:
    """Build jobs from any JSON-LD ``JobPosting`` objects on the page.

    Args:
        soup: The parsed page.
        page_url: URL the page came from, for resolving relative links.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        career_page_url: Board URL recorded on each job.

    Returns:
        One entry per posting; unusable postings are ``None``.
    """
    collected: List[Optional[Job]] = []

    for posting in json_ld_job_postings(soup):
        title = clean_text(posting.get("title") or posting.get("name") or "")
        url = posting.get("url") or posting.get("@id") or posting.get("sameAs") or ""

        collected.append(
            build_job(
                company_name=company_name,
                title=title,
                job_url=absolute_url(page_url, str(url)),
                location=_location_from_json_ld(posting),
                career_page_url=career_page_url or page_url,
                platform=platform,
            )
        )

    return collected


def _location_from_json_ld(posting: Dict[str, Any]) -> str:
    """Read a location out of a JSON-LD ``JobPosting``.

    Args:
        posting: The decoded posting object.

    Returns:
        ``"Locality, Region, Country"`` with missing parts omitted.
    """
    node = posting.get("jobLocation")
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        remote = posting.get("applicantLocationRequirements")
        if isinstance(remote, dict):
            return clean_text(remote.get("name") or "")
        return ""

    address = node.get("address")
    if isinstance(address, list):
        address = address[0] if address else None
    if isinstance(address, str):
        return clean_text(address)
    if not isinstance(address, dict):
        return clean_text(node.get("name") or "")

    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name") or country.get("identifier") or ""

    parts = [
        clean_text(address.get("addressLocality") or ""),
        clean_text(address.get("addressRegion") or ""),
        clean_text(country or ""),
    ]
    return ", ".join(part for part in parts if part)


def _jobs_from_microdata(
    soup: BeautifulSoup, page_url: str, company_name: str, platform: str, career_page_url: str
) -> List[Optional[Job]]:
    """Build jobs from inline microdata ``JobPosting`` annotations.

    Args:
        soup: The parsed page.
        page_url: URL the page came from.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        career_page_url: Board URL recorded on each job.

    Returns:
        One entry per posting; unusable postings are ``None``.
    """
    collected: List[Optional[Job]] = []

    for posting in job_postings_from_microdata(soup):
        collected.append(
            build_job(
                company_name=company_name,
                title=posting.get("title") or posting.get("name") or "",
                job_url=absolute_url(page_url, posting.get("url") or ""),
                location=posting.get("jobLocation") or posting.get("addressLocality") or "",
                career_page_url=career_page_url or page_url,
                platform=platform,
            )
        )

    return collected


def _candidate_links(soup: BeautifulSoup, page_url: str) -> List[Tuple[Tag, str, str]]:
    """Collect links that could be job postings.

    Args:
        soup: The parsed page.
        page_url: URL the page came from.

    Returns:
        ``(anchor, title, absolute_url)`` for every plausible posting link.
    """
    candidates: List[Tuple[Tag, str, str]] = []

    for anchor in soup.find_all("a", href=True):
        url = absolute_url(page_url, anchor.get("href"))
        if not url or not _looks_like_job_url(url):
            continue

        title = clean_text(anchor)
        if not title:
            # Some cards wrap an image or heading; take the heading's text.
            heading = anchor.find(["h1", "h2", "h3", "h4", "h5", "span", "div"])
            title = clean_text(heading) if heading else ""

        if not _looks_like_title(title):
            continue

        candidates.append((anchor, title, url))

    return candidates


def _jobs_from_cards(
    soup: BeautifulSoup, page_url: str, company_name: str, platform: str, career_page_url: str
) -> List[Optional[Job]]:
    """Build jobs by detecting the page's repeating job list.

    Links are grouped by their structural position; the largest group is taken
    as the job list. When no group repeats enough to be conclusive, every
    plausible link is used instead, which suits pages listing only a job or two.

    Args:
        soup: The parsed page.
        page_url: URL the page came from.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        career_page_url: Board URL recorded on each job.

    Returns:
        One entry per posting; unusable postings are ``None``.
    """
    candidates = _candidate_links(soup, page_url)
    if not candidates:
        return []

    groups: Dict[Tuple[Any, ...], List[Tuple[Tag, str, str]]] = defaultdict(list)
    for anchor, title, url in candidates:
        groups[_signature(anchor)].append((anchor, title, url))

    largest = max(groups.values(), key=len)
    chosen = largest if len(largest) >= _MIN_GROUP else candidates

    logger.debug(
        "Generic: {} candidate link(s) in {} group(s); using {}",
        len(candidates),
        len(groups),
        len(chosen),
    )

    collected: List[Optional[Job]] = []
    for anchor, title, url in chosen:
        collected.append(
            build_job(
                company_name=company_name,
                title=title,
                job_url=url,
                location=_location_in(_card_of(anchor), title),
                career_page_url=career_page_url or page_url,
                platform=platform,
            )
        )

    return collected


def extract_jobs(
    markup: str,
    page_url: str,
    company_name: str,
    platform: str = PLATFORM,
    career_page_url: str = "",
) -> List[Job]:
    """Extract every posting from one page of HTML.

    Tries structured data first, then falls back to detecting the page's
    repeating job cards. Never raises: a page it cannot read yields an empty
    list.

    Args:
        markup: Raw HTML.
        page_url: URL the markup came from, used to resolve relative links.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column. Platform adapters pass
            their own name when using this as a fallback.
        career_page_url: Board URL recorded on each job. Defaults to
            ``page_url``.

    Returns:
        The postings found, deduplicated. Empty when the page has none.
    """
    try:
        soup = parse_html(markup)

        for strategy, extract in (
            ("JSON-LD", _jobs_from_json_ld),
            ("microdata", _jobs_from_microdata),
            ("job cards", _jobs_from_cards),
            ("apply links", _jobs_from_apply_links),
        ):
            jobs = dedupe(extract(soup, page_url, company_name, platform, career_page_url))
            if jobs:
                logger.debug("Generic: {} job(s) via {} on {}", len(jobs), strategy, page_url)
                return jobs

        # Last: the JSON the page ships to its own JavaScript. Takes the raw
        # markup rather than the soup so inline scripts are read verbatim.
        jobs = jobs_from_state(
            markup,
            page_url,
            company_name,
            platform,
            career_page_url=career_page_url or page_url,
            soup=soup,
        )
        if jobs:
            logger.debug("Generic: {} job(s) via embedded state on {}", len(jobs), page_url)
            return jobs

        logger.debug("Generic: nothing extractable on {}", page_url)
        return []
    except Exception:  # noqa: BLE001 - extraction must never end a crawl
        logger.opt(exception=True).warning("Generic: extraction failed on {}", page_url)
        return []


def _next_page_url(soup: BeautifulSoup, page_url: str, seen: Sequence[str]) -> str:
    """Find the link to the next page of a listing, if there is one.

    Args:
        soup: The parsed page.
        page_url: URL the page came from.
        seen: URLs already visited, so a cyclic pager terminates.

    Returns:
        The next page's URL, or ``""``.
    """
    host = (urlsplit(page_url).hostname or "").lower()

    candidates = soup.find_all("a", attrs={"rel": "next"}, href=True)
    candidates += [
        anchor
        for anchor in soup.find_all("a", href=True)
        if clean_text(anchor).lower() in {"next", "next page", "next »", "›", "»"}
    ]

    for anchor in candidates:
        url = absolute_url(page_url, anchor.get("href"))
        if not url or url in seen:
            continue
        if (urlsplit(url).hostname or "").lower() != host:
            continue
        return url

    # Numbered pagers ("1 2 3 ›") name no "next": take the lowest unvisited
    # page number, which walks the list in order across successive calls.
    numbered: List[Tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        text = clean_text(anchor)
        if not text.isdigit() or not 1 < int(text) <= 200:
            continue
        url = absolute_url(page_url, anchor.get("href"))
        if not url or url in seen:
            continue
        if (urlsplit(url).hostname or "").lower() != host:
            continue
        numbered.append((int(text), url))

    if numbered:
        return min(numbered)[1]

    return ""


def jobs_from_rendered_page(
    page: Any,
    company_name: str,
    platform: str = PLATFORM,
    career_page_url: str = "",
) -> List[Job]:
    """Extract postings from a :class:`~utils.browser.RenderedPage`.

    The board's own XHR responses are mined first: a page that fetched its
    listings as JSON has already done the parsing work, and that payload is
    both cleaner and more complete than the DOM rendered from it. The rendered
    DOM is the fallback.

    Args:
        page: The rendered page.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        career_page_url: Board URL recorded on each job.

    Returns:
        The postings found, deduplicated. Empty when the render revealed none.
    """
    if page is None or not page.ok:
        return []

    board = career_page_url or page.url

    # Every captured response is mined, not just the first that yields
    # postings: an infinitely-scrolling board fetches a page at a time, so its
    # listings arrive across many responses and stopping at the first would cap
    # the company at one page.
    collected = []
    for payload in page.payloads:
        collected.extend(
            jobs_from_payload(payload, page.url, company_name, platform, career_page_url=board)
        )

    jobs = dedupe(collected)
    if jobs:
        logger.debug("Generic: {} job(s) from captured XHR on {}", len(jobs), page.url)
        return jobs

    return extract_jobs(page.html, page.url, company_name, platform, career_page_url=board)


def render_and_extract(
    url: str,
    company_name: str,
    platform: str = PLATFORM,
    career_page_url: str = "",
    screenshot_path: Optional[str] = None,
) -> List[Job]:
    """Load a page in a browser and extract whatever it renders.

    Args:
        url: Page to visit.
        company_name: Company as named in the input sheet.
        platform: Label for the ``Platform`` column.
        career_page_url: Board URL recorded on each job.
        screenshot_path: Where to write a diagnostic screenshot, if wanted.

    Returns:
        The postings found. Empty when the run forbids the browser, no browser
        is installed, or the render revealed nothing.
    """
    if not SETTINGS.browser_fallback:
        return []

    # Imported here so that a machine without Playwright pays nothing at import.
    from utils.browser import render

    logger.info("Generic: retrying {} in a browser for {!r}", url, company_name)
    page = render(url, screenshot_path=screenshot_path)
    if page is None:
        logger.debug("Generic: no browser available for {}", url)
        return []

    return jobs_from_rendered_page(page, company_name, platform, career_page_url or url)


def fetch_jobs(
    career_url: str,
    company_name: str,
    session: Optional[requests.Session] = None,
    use_browser: Optional[bool] = None,
) -> List[Job]:
    """Fetch and extract postings from an unrecognised career page.

    Follows a listing's "next page" links while they keep yielding new postings,
    up to a bounded number of pages. When the whole HTTP pass finds nothing —
    which usually means the listings are painted by JavaScript — the page is
    loaded once more in headless Chromium.

    Args:
        career_url: The career page to read.
        company_name: Company as named in the input sheet.
        session: Session to reuse. One is built and closed here when omitted.
        use_browser: Whether to allow the browser retry. Defaults to the run's
            :data:`~config.settings.SETTINGS` value; pass ``False`` to forbid
            it for one call, as the tests do.

    Returns:
        Every posting found, deduplicated. Empty when neither HTML nor a
        rendered browser page publishes any.

    Raises:
        AdapterHttpError: If the page cannot be fetched at all and no browser
            render succeeded either — a dead link stays distinguishable from an
            empty board.
    """
    url = str(career_url or "").strip()
    if not url:
        return []
    if "://" not in url:
        url = f"https://{url}"

    logger.info("Generic: reading {} for {!r}", url, company_name)

    browser_allowed = SETTINGS.browser_fallback if use_browser is None else use_browser

    owned = session is None
    http = session if session is not None else build_session()

    collected: List[Job] = []
    visited: List[str] = []
    current = url
    transport_error: Optional[Exception] = None

    try:
        for page in range(1, _MAX_PAGES + 1):
            try:
                markup = get_text(http, current)
            except Exception as exc:  # noqa: BLE001 - a browser may still succeed
                if page > 1:
                    logger.debug("Generic: page {} unavailable, treating as the end", page)
                    break
                transport_error = exc
                break

            visited.append(current)

            found = extract_jobs(markup, current, company_name, PLATFORM, career_page_url=url)
            before = len(collected)
            collected = dedupe(collected + found)

            if len(collected) == before:
                # This page added nothing new; another page will not either.
                break

            try:
                current = _next_page_url(parse_html(markup), current, visited)
            except Exception:  # noqa: BLE001 - pagination is best effort
                logger.opt(exception=True).debug("Generic: could not read pagination on {}", current)
                break

            if not current:
                break

            logger.debug("Generic: following page {} to {}", page + 1, current)
    finally:
        if owned:
            http.close()

    if not collected and browser_allowed:
        rendered = render_and_extract(url, company_name, PLATFORM, career_page_url=url)
        if rendered:
            logger.success("Generic: {} job(s) for {!r} via the browser", len(rendered), company_name)
            return rendered

    if not collected and transport_error is not None:
        # Nothing was salvaged, so the transport failure is the real outcome.
        raise transport_error

    logger.success("Generic: {} job(s) for {!r}", len(collected), company_name)
    return collected
