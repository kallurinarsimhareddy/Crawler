"""Turn a company name and a website into the board URL an adapter can read.

The master sheet's minimum viable row is two cells::

    Company Name | Website
    OPKO Health  | https://www.opko.com

Everything else — the careers page, the applicant tracking system, the tenant's
actual job-board URL — is the crawler's job to work out::

    >>> from crawler.resolve import resolve_company
    >>> resolution = resolve_company({"company": "OPKO Health", "website": "https://www.opko.com"})
    >>> resolution.it_link
    'https://myjobs.adp.com/opko/cx/job-listing'
    >>> resolution.platform.value
    'ADP'

**Almost none of this is new.** :func:`crawler.career_finder.find_careers_url`
already reads a company's site, scores its links, follows the careers link one
hop further, and returns the applicant tracking system's URL when it finds one —
which is precisely the ``website -> careers page -> ADP board`` chain above.
:func:`crawler.platform_detector.detect_platform` already names the platform
from that URL. This module orchestrates the two and records what it learned.

It exists for three reasons the existing pieces do not cover.

**The sheet needs both URLs, not just the best one.** ``find_careers_url``
returns one URL — the most useful. ``MASTER_COMPANIES`` has a column for the
careers page *and* a column for the board, and the operator wants to see both.

**Resolution should happen once, not every week.** Writing what was discovered
back to the sheet means next Friday's run reads the board URL from a cell
instead of re-crawling a marketing site to re-derive it. A run over a resolved
sheet does no discovery at all.

**A value the operator typed is never second-guessed.** If a row already names a
careers page or a board, that is used as-is. Discovery fills blanks; it does not
correct entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from crawler.platform_detector import Platform, detect_platform, normalise_url
from utils.html import absolute_url, parse_html
from utils.http import AdapterError, get_text

__all__ = [
    "Resolution",
    "ats_link_on",
    "is_ats",
    "resolve_company",
]

#: Platforms that mean "no applicant tracking system was identified".
#: ``GENERIC_HTML`` is a valid thing to crawl but names no vendor, so it is not
#: an ATS for the purpose of filling the ``IT Link`` column.
_NOT_AN_ATS: Final[frozenset] = frozenset({Platform.UNKNOWN, Platform.GENERIC_HTML})

#: How many links on a careers page to consider when hunting for a board.
#: A careers page has tens of links; the board is invariably among the first
#: few that leave the site.
_MAX_LINKS: Final[int] = 200

#: Where the resolution came from.
SOURCE_SHEET: Final[str] = "sheet"
SOURCE_DISCOVERED: Final[str] = "discovered"
SOURCE_NONE: Final[str] = "unresolved"


def is_ats(platform: Platform) -> bool:
    """Whether a platform names a real applicant tracking system.

    Args:
        platform: What the detector reported.

    Returns:
        ``True`` for a named vendor. ``Generic HTML`` is crawlable but names no
        vendor, so it does not belong in the ``IT Link`` column.
    """
    return platform not in _NOT_AN_ATS


@dataclass(frozen=True)
class Resolution:
    """What could be worked out about where a company's jobs live.

    Attributes:
        company_key: The company this is for.
        career_url: Its careers page, as given or as discovered.
        it_link: Its applicant tracking system's board URL, when one was
            identified. Empty when the careers page is the board.
        platform: The platform detected for whichever URL will be crawled.
        source: ``"sheet"`` when the row already said, ``"discovered"`` when
            the crawler worked it out, ``"unresolved"`` when it could not.
        detail: One line explaining the outcome, for the log and the report.
    """

    company_key: str = ""
    career_url: str = ""
    it_link: str = ""
    platform: Platform = Platform.UNKNOWN
    source: str = SOURCE_NONE
    detail: str = ""

    @property
    def resolved(self) -> bool:
        """Whether there is anything to crawl.

        Returns:
            ``True`` when a URL was established.
        """
        return bool(self.it_link or self.career_url)

    @property
    def discovered(self) -> bool:
        """Whether the crawler worked this out rather than being told.

        Returns:
            ``True`` when discovery produced the URLs.
        """
        return self.source == SOURCE_DISCOVERED

    @property
    def best_url(self) -> str:
        """The URL an adapter should be pointed at.

        Returns:
            The board where one is known, else the careers page.
        """
        return self.it_link or self.career_url

    def to_record(self, record: Mapping[str, str]) -> dict:
        """Merge the resolution into a company record for the crawl engine.

        Args:
            record: The company as the master list holds it.

        Returns:
            The record with ``career_url`` and ``it_link`` filled in, in the
            shape :meth:`crawler.crawler_engine.CrawlerEngine.crawl_company`
            expects. The engine tries ``it_link`` first, so a resolved board is
            crawled directly rather than rediscovered.
        """
        merged = dict(record)
        if self.career_url:
            merged["career_url"] = self.career_url
        if self.it_link:
            merged["it_link"] = self.it_link
        return merged

    def sheet_updates(self) -> dict:
        """The ``MASTER_COMPANIES`` cells this resolution fills in.

        Returns:
            Field name to value, omitting anything that was not established so
            a blank never overwrites a stored value.
        """
        updates = {}
        if self.career_url:
            updates["career_url"] = self.career_url
        if self.it_link:
            updates["it_link"] = self.it_link
        if is_ats(self.platform):
            updates["platform"] = self.platform.value
        return updates


def ats_link_on(page_url: str, session: object) -> str:
    """Find a link on a page that leads to an applicant tracking system.

    Used when the sheet already names a careers page: the page is the
    operator's answer to "where are the jobs", and the board it links out to is
    what an adapter actually needs.

    Args:
        page_url: The careers page to read.
        session: HTTP session to use.

    Returns:
        The board's URL, or ``""`` when the page links to none. Never raises:
        a page that cannot be read simply yields nothing.
    """
    if not page_url or session is None:
        return ""

    try:
        markup = get_text(session, page_url)
    except AdapterError as exc:
        logger.debug("Could not read {} while hunting for a board: {}", page_url, exc)
        return ""
    except Exception:  # noqa: BLE001 - resolution is best effort
        logger.opt(exception=True).debug("Unexpected failure reading {}", page_url)
        return ""

    if not markup:
        return ""

    try:
        soup = parse_html(markup)
    except Exception:  # noqa: BLE001 - malformed markup is not fatal
        return ""

    for anchor in soup.find_all("a", href=True)[:_MAX_LINKS]:
        candidate = absolute_url(page_url, anchor["href"])
        if not candidate:
            continue

        if is_ats(detect_platform(candidate)):
            logger.info("Resolution: {} links out to {}", page_url, candidate)
            return candidate

    return ""


def resolve_company(
    record: Mapping[str, str],
    session: Optional[object] = None,
    discover: bool = True,
) -> Resolution:
    """Work out where one company's jobs are published.

    Args:
        record: A company record carrying at least ``company`` and one of
            ``website``, ``career_url`` or ``it_link``. This is the shape
            :meth:`sheets.companies.CompanyRepository.crawl_records` produces.
        session: HTTP session for discovery. Without one, only what the record
            already says is used — no network call is made.
        discover: Whether to search the company's site at all.

    Returns:
        What was established. Never raises: a company that cannot be resolved
        yields an unresolved result and the run moves on.
    """
    company_key = str(record.get("company_key") or "")
    name = str(record.get("company") or record.get("company_name") or "")

    website = normalise_url(str(record.get("website") or ""))
    career_url = normalise_url(str(record.get("career_url") or ""))
    it_link = normalise_url(str(record.get("it_link") or ""))

    # 1. The row already names a board. Nothing to work out.
    if it_link:
        platform = detect_platform(it_link)
        if is_ats(platform):
            return Resolution(
                company_key=company_key,
                career_url=career_url or it_link,
                it_link=it_link,
                platform=platform,
                source=SOURCE_SHEET,
                detail=f"IT Link in the sheet is {platform.value}",
            )

    # 2. The row names a careers page. Use it, and see whether it leads to a
    #    board — but never replace what the operator typed.
    if career_url:
        platform = detect_platform(career_url)

        if is_ats(platform):
            return Resolution(
                company_key=company_key,
                career_url=career_url,
                it_link=it_link or career_url,
                platform=platform,
                source=SOURCE_SHEET,
                detail=f"Career Page URL in the sheet is {platform.value}",
            )

        found = ats_link_on(career_url, session) if (discover and session) else ""
        if found:
            found_platform = detect_platform(found)
            return Resolution(
                company_key=company_key,
                career_url=career_url,
                it_link=found,
                platform=found_platform,
                source=SOURCE_DISCOVERED,
                detail=f"careers page links to {found_platform.value}",
            )

        return Resolution(
            company_key=company_key,
            career_url=career_url,
            it_link=it_link,
            platform=platform,
            source=SOURCE_SHEET,
            detail="crawling the careers page directly",
        )

    # 3. Only a website. This is the minimum viable row, and the case the whole
    #    module exists for.
    if website and discover and session is not None:
        found = _discover(website, session)

        if found:
            platform = detect_platform(found)
            return Resolution(
                company_key=company_key,
                career_url=found,
                # A discovered URL on a vendor's host is the board; one on the
                # company's own domain is a careers page and nothing more.
                it_link=found if is_ats(platform) else "",
                platform=platform,
                source=SOURCE_DISCOVERED,
                detail=(
                    f"discovered {platform.value} board from the website"
                    if is_ats(platform)
                    else "discovered a careers page on the company's own site"
                ),
            )

        logger.info("{}: no careers page found from {}", name or company_key, website)
        return Resolution(
            company_key=company_key,
            platform=Platform.UNKNOWN,
            source=SOURCE_NONE,
            detail="no careers page could be found from the website",
        )

    # 4. A website, but discovery is switched off. The engine tries `website`
    #    as its own last-resort seed, so nothing needs to be filled in here --
    #    and career_url is deliberately left blank rather than set to the
    #    homepage, because writing a marketing URL into the Career Page URL
    #    column would put a wrong answer in the sheet and keep it there.
    if website:
        return Resolution(
            company_key=company_key,
            platform=detect_platform(website),
            source=SOURCE_SHEET,
            detail="discovery is off; the engine will crawl the website itself",
        )

    return Resolution(
        company_key=company_key,
        source=SOURCE_NONE,
        detail="no website, careers page or board URL in MASTER_COMPANIES",
    )


def _discover(website: str, session: object) -> str:
    """Search a company's site for its careers page or board.

    Args:
        website: The company's website.
        session: HTTP session to use.

    Returns:
        The best URL found, or ``""``.
    """
    # Imported here so a caller that never discovers does not pull in the
    # HTTP stack career_finder brings with it.
    from crawler.career_finder import find_careers_url

    try:
        return find_careers_url(website, session)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - discovery is best effort
        logger.opt(exception=True).debug("Discovery failed for {}", website)
        return ""
