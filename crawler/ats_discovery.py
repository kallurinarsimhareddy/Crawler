"""Fill in the ``IT Link`` column: find each company's actual job board.

    python -m crawler.ats_discovery --dry-run
    python -m crawler.ats_discovery --dry-run --limit 25 --workers 6
    python -m crawler.ats_discovery --apply

``MASTER_COMPANIES`` currently holds 109 companies of which 103 name no board
at all. Every weekly run therefore rediscovers those boards from scratch, and
throws the answer away. This stage works the board out once and stores it, so
next week's run reads a cell instead of crawling a marketing site.

**No new detector.** The chain — website to careers page to vendor board — is
:func:`crawler.resolve.resolve_company`, which is already what the weekly run
uses, and the vendor is named by
:func:`crawler.platform_detector.detect_platform`. This module orchestrates
those, adds one hop they do not take, and applies the rules the column needs.

The hop it adds exists because of the shape this sheet is actually in. Its
``Website`` column is blank and its ``Career Page URL`` column holds each
company's **home page**, not a careers page. Resolution therefore takes its
"the row names a careers page" branch, scans that one page for an outbound
vendor link, and stops — and the richer
:func:`~crawler.career_finder.find_careers_url` chain never runs at all,
because that one keys off ``Website``. Most companies link ``Careers`` to an
internal page rather than straight out to their ATS, so that single scan finds
nothing. :func:`_hunt_for_board` runs the full chain explicitly against
whichever URL the row does have, then follows the page it returns one further
hop. On the live sheet that is the difference between 0 boards found and 20.

Four rules govern what may be written, and each is a separate guard rather than
a comment, because each is a way to quietly corrupt a column an operator
maintains by hand:

**A stored board is authoritative.** A row that already names a real vendor is
kept, costs no request, and produces no update — even if the company's site now
links somewhere else. Discovery fills blanks; it does not correct entries.

**"Demonstrably invalid" means unparseable, not merely unexpected.** The only
stored value this stage will replace is one that is not a usable URL at all —
the ``N/A`` and ``TBD`` filler that sheets accumulate. A working link to a
vendor nobody expected is still the operator's answer.

**A board URL goes in ``IT Link`` and nowhere else.** :meth:`Enrichment.sheet_updates`
returns at most ``it_link`` and ``platform``. It deliberately does not return
``career_url``, even though discovery usually learns one on the way, because
writing a board URL into the careers column is how the two columns stop meaning
different things.

**A board that cannot be named is not written.** ``Generic HTML`` is crawlable
but names no vendor, so it never fills this column; the company is reported as
unresolved with the reason attached.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any, Callable, Dict, Final, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, parse_qsl, urlsplit

from loguru import logger

from config.settings import SETTINGS, configure
from crawler.platform_detector import Platform, detect_platform, normalise_url
from crawler.resolve import is_ats, resolve_company
from utils.html import absolute_url, parse_html
from utils.blocking import Block, classify_text
from utils.browser import render as render_page
from utils.http import build_session, get_text  # noqa: F401 - patched in tests

__all__ = [
    "AGGREGATORS",
    "STATUS_BLOCKED",
    "STATUS_DISCOVERED",
    "STATUS_KEPT",
    "STATUS_REJECTED",
    "STATUS_REPLACED",
    "STATUS_UNRESOLVED",
    "DiscoveryReport",
    "Enrichment",
    "apply_discoveries",
    "discover_board",
    "discover_missing_boards",
    "main",
    "needs_a_board",
    "roster_with_rows",
]

#: The row already named a real vendor. Kept, untouched, uncharged.
STATUS_KEPT: Final[str] = "kept"

#: A board was found for a row that had none.
STATUS_DISCOVERED: Final[str] = "discovered"

#: The stored value was not a URL, and a real board was found to put there.
STATUS_REPLACED: Final[str] = "replaced"

#: Nothing could be confidently identified. The reason says what happened.
STATUS_UNRESOLVED: Final[str] = "unresolved"

#: The company's site could not be read at all.
STATUS_BLOCKED: Final[str] = "blocked"

#: A vendor was identified and the URL is still not fit to store: an
#: aggregator, a link to one posting, or a vendor's sign-in application. These
#: are reported separately from :data:`STATUS_UNRESOLVED` because "we found
#: nothing" and "we found something and refused it" are different facts, and
#: only the second one is worth a human's attention.
STATUS_REJECTED: Final[str] = "rejected"

#: Companies examined at once. Modest by default: these are third-party
#: marketing sites, mostly one company per host, and this is not a race.
DEFAULT_WORKERS: Final[int] = 6

#: Platforms that list a company's jobs without being that company's applicant
#: tracking system. A link to one is a real, crawlable board and is reported as
#: a discovery, but it is separated in the report because storing an aggregator
#: as a company's ``IT Link`` is a judgement call an operator should make.
AGGREGATORS: Final[frozenset] = frozenset({Platform.INDEED})

#: Hosts that belong to a vendor but serve its signed-in application rather
#: than a public job board. A URL here is detected as the vendor and is still
#: not something an adapter can read, so it is refused rather than stored.
#:
#: Workday is the case this exists for: public boards live on
#: ``myworkdayjobs.com`` and ``myworkdaysite.com``, while ``myworkday.com`` is
#: the authenticated tenant app. A careers page that deep-links into it yields
#: a URL the Workday adapter parses as tenant ``www`` and cannot fetch.
_APPLICATION_HOSTS: Final[Tuple[Tuple[Platform, str], ...]] = (
    (Platform.WORKDAY, "myworkday.com"),
)

#: File extensions that make a URL an asset rather than a page. A vendor's CDN
#: serves its widget from its own domain, so the URL detects as the vendor and
#: is a stylesheet.
_ASSET_SUFFIXES: Final[Tuple[str, ...]] = (
    ".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
)

#: Path fragments that mark a vendor's shared static bundle, which names the
#: vendor but never the tenant — the tenant arrives at runtime.
_ASSET_PATHS: Final[Tuple[str, ...]] = ("/__assets__/", "/job-widget/", "/static/", "/assets/")

#: Absolute URLs inside inline script text. Several boards are named only here:
#: Pet Supplies Plus publishes its iCIMS portal in a JavaScript object and
#: links to it from nowhere in the markup.
_URL_IN_SCRIPT: Final[re.Pattern[str]] = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{12,300}"
)

#: Where a board conventionally lives on a company's own domain, in the order
#: worth trying. Deliberately short: this runs once per company that has no
#: board, and every entry is a request.
_CAREERS_PATHS: Final[Tuple[str, ...]] = (
    "/careers", "/jobs", "/careers/jobs", "/about/careers", "/employment",
    "/join-us", "/work-with-us", "/careers/open-positions",
)

#: Subdomains that conventionally host a board. Georgetown's Workday board and
#: San Diego State's iCIMS portal are both linked from ``jobs.<domain>`` and
#: from nowhere on the main site.
_CAREERS_SUBDOMAINS: Final[Tuple[str, ...]] = ("jobs", "careers", "employment")

#: Ceiling on probe requests per company, so a company with no board anywhere
#: costs a bounded number of requests rather than one per candidate.
MAX_PROBES: Final[int] = 8

#: Path segments that sit where a tenant would and are not one.
_NOT_A_TENANT: Final[frozenset] = frozenset({
    "job-widget", "__assets__", "static", "assets", "js", "css", "api",
    "embed", "jobs", "careers", "search", "www",
})

#: Pages a browser visit is worth spending on, in order. A render costs
#: seconds where a fetch costs milliseconds, so only one page per company is
#: visited and this decides which.
_RENDER_TARGETS: Final[int] = 2

#: Ceiling on references read from one page, matching the one
#: :mod:`crawler.resolve` already applies to anchors.
_MAX_LINKS: Final[int] = 200


def needs_a_board(record: Mapping[str, str]) -> bool:
    """Whether a row's ``IT Link`` is missing or unusable.

    Args:
        record: A company record from
            :meth:`sheets.companies.CompanyRepository.prepare_roster`.

    Returns:
        ``True`` when the cell is blank, whitespace, or filler that is not a
        URL. A cell naming any real vendor returns ``False`` — that row is
        already answered and is not examined again.
    """
    raw = str(record.get("it_link") or "").strip()
    if not raw:
        return True

    # detect_platform is what rejects filler like "N/A" and "none found":
    # normalise_url would happily turn those into "https://N/A", a
    # syntactically valid URL with a nonsense hostname. UNKNOWN means the cell
    # holds no usable URL at all.
    return detect_platform(raw) is Platform.UNKNOWN


@dataclass(frozen=True)
class Enrichment:
    """What was established about one company's board.

    Attributes:
        company_key: The company this is for.
        company: Its name, for the report.
        previous_it_link: Whatever the cell held before, verbatim.
        it_link: The board that was identified. Empty when none was.
        platform: The vendor behind :attr:`it_link`.
        status: One of the ``STATUS_*`` constants.
        reason: One line explaining the outcome. Always set for anything that
            is not a plain discovery, because a column left unfilled is only
            useful if it says why.
        seconds: How long this company took.
        career_url: The careers page discovery passed through, recorded for the
            report only — it is never written back.
    """

    company_key: str = ""
    company: str = ""
    previous_it_link: str = ""
    it_link: str = ""
    platform: Platform = Platform.UNKNOWN
    status: str = STATUS_UNRESOLVED
    reason: str = ""
    seconds: float = 0.0
    career_url: str = ""
    row: int = 0

    @property
    def changed(self) -> bool:
        """Whether this company would cause a write.

        Returns:
            ``True`` for a discovery or a replacement.
        """
        return self.status in (STATUS_DISCOVERED, STATUS_REPLACED)

    @property
    def storable(self) -> bool:
        """Whether this may be written to the spreadsheet.

        Every condition the board has to meet, restated in one place so a
        caller cannot satisfy some of them and forget the rest:

        * a board was discovered, or replaced demonstrably invalid filler;
        * the URL is present and its vendor is confidently named;
        * the vendor is not an aggregator;
        * the URL is not a vendor's sign-in application;
        * the URL names a board rather than a single posting;
        * and the row it belongs to is known.

        The first condition already implies a blank or unusable previous value,
        because a row that named a real board is never examined.

        Returns:
            ``True`` when it is safe to write.
        """
        return (
            self.changed
            and bool(self.it_link)
            and is_ats(self.platform)
            and self.platform not in AGGREGATORS
            and _is_usable_board(self.it_link, self.platform)
            and not _names_one_posting(self.it_link)
            and self.row > 0
        )

    def sheet_updates(self) -> Dict[str, str]:
        """The ``MASTER_COMPANIES`` cells this would fill in.

        Returns:
            ``it_link`` and ``platform``, or an empty mapping when nothing was
            established. **Never** ``website``, ``career_url``, ``company_name``
            or ``company_key``: a board URL belongs in exactly one column, and
            the careers page discovery passed through on the way is not this
            stage's to write.

            Note this deliberately does not test :attr:`row`, so a caller can
            see the intended cells for a record read without one. The write
            path gates on :attr:`storable`, which does.
        """
        if not self.changed or not self.it_link or not is_ats(self.platform):
            return {}
        if self.platform in AGGREGATORS or not _is_usable_board(self.it_link, self.platform):
            return {}
        if _names_one_posting(self.it_link):
            return {}
        return {"it_link": self.it_link, "platform": self.platform.value}


@dataclass
class DiscoveryReport:
    """What a pass over the master list found.

    Attributes:
        examined: Companies actually looked at.
        skipped_have_board: Rows left alone because they already named a board.
        entries: One record per examined company.
        seconds: Wall-clock time for the pass.
        sheet_reads: Google Sheets read calls spent.
        sheet_writes: Google Sheets write calls spent. Zero on a dry run.
    """

    examined: int = 0
    skipped_have_board: int = 0
    entries: List[Enrichment] = field(default_factory=list)
    seconds: float = 0.0
    sheet_reads: int = 0
    sheet_writes: int = 0
    renders_spent: int = 0

    @property
    def discovered(self) -> int:
        """How many companies gained a board that may be stored."""
        return sum(1 for entry in self.entries if entry.storable)

    @property
    def rejected(self) -> List[Enrichment]:
        """Companies where a vendor was found but the URL was refused."""
        return [entry for entry in self.entries if entry.status == STATUS_REJECTED]

    @property
    def unresolved(self) -> List[Enrichment]:
        """Companies examined that yielded nothing."""
        return [entry for entry in self.entries if entry.status == STATUS_UNRESOLVED]

    @property
    def failed(self) -> List[Enrichment]:
        """Companies whose site could not be read."""
        return [entry for entry in self.entries if entry.status == STATUS_BLOCKED]

    @property
    def platforms(self) -> Dict[str, int]:
        """Vendor name to how many boards were found on it."""
        counts: Dict[str, int] = {}
        for entry in self.entries:
            if entry.changed:
                counts[entry.platform.value] = counts.get(entry.platform.value, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    @property
    def blockers(self) -> Dict[str, int]:
        """Blocker label to how many companies hit it."""
        counts: Dict[str, int] = {}
        for entry in self.failed:
            counts[entry.reason] = counts.get(entry.reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def planned_updates(self) -> List[Dict[str, str]]:
        """The writes a live run would make, as records.

        Returns:
            One record per changed company, carrying its key and the cells to
            fill. Empty on a pass that established nothing.
        """
        return [
            {"row": entry.row, "company": entry.company,
             "company_key": entry.company_key, **entry.sheet_updates()}
            for entry in self.entries
            if entry.storable
        ]

    def render(self, dry_run: bool = True) -> str:
        """Render the report for a terminal.

        Args:
            dry_run: Whether anything was written.

        Returns:
            The report as text.
        """
        rule = "=" * 96
        lines = [
            rule,
            "ATS DISCOVERY — DRY RUN (nothing written)" if dry_run else "ATS DISCOVERY — APPLIED",
            rule,
            "",
            f"  companies examined      {self.examined}",
            f"  already had a board     {self.skipped_have_board}",
            f"  boards discovered       {self.discovered}",
            f"  found but refused       {len(self.rejected)}",
            f"  unresolved              {len(self.unresolved)}",
            f"  blocked / unreadable    {len(self.failed)}",
            f"  browser visits spent    {self.renders_spent}",
            f"  runtime                 {self.seconds:.1f}s",
            f"  Google Sheet reads      {self.sheet_reads}",
            f"  Google Sheet writes     {self.sheet_writes}",
            "",
        ]

        if self.platforms:
            lines += [rule, "PLATFORMS DISCOVERED", rule]
            for name, count in self.platforms.items():
                lines.append(f"  {name:<28}{count:>5}")
            lines.append("")

        found = [entry for entry in self.entries if entry.storable]
        if found:
            lines += [rule, "BOARDS DISCOVERED (would be stored)", rule]
            for entry in sorted(found, key=lambda item: item.company.lower()):
                marker = " (replaced filler)" if entry.status == STATUS_REPLACED else ""
                lines.append(
                    f"  row {entry.row:>4}  {entry.company[:28]:<29} "
                    f"{entry.platform.value:<14}{marker}"
                )
                lines.append(f"      {entry.it_link[:86]}")
            lines.append("")

        if self.rejected:
            lines += [
                rule,
                "FOUND BUT REFUSED (IT Link left unchanged)",
                rule,
            ]
            for entry in sorted(self.rejected, key=lambda item: item.company.lower()):
                lines.append(f"  {entry.company[:28]:<29} {entry.reason[:60]}")
                lines.append(f"      {entry.it_link[:86]}")
            lines.append("")

        if self.unresolved:
            lines += [rule, "UNRESOLVED (IT Link left unchanged)", rule]
            for entry in sorted(self.unresolved, key=lambda item: item.company.lower()):
                lines.append(f"  {entry.company[:30]:<31} {entry.reason[:56]}")
            lines.append("")

        if self.failed:
            lines += [rule, "BLOCKED / UNREADABLE", rule]
            for entry in sorted(self.failed, key=lambda item: item.company.lower()):
                lines.append(f"  {entry.company[:30]:<31} {entry.reason[:56]}")
            lines.append("")

        return "\n".join(lines)


def discover_board(
    record: Mapping[str, str],
    session: Optional[object] = None,
    discover: bool = True,
    render_budget: Optional["_Budget"] = None,
) -> Enrichment:
    """Work out one company's board URL.

    Args:
        record: A company record carrying at least ``company`` and one of
            ``website``, ``career_url`` or ``it_link``.
        session: HTTP session for discovery.
        discover: Whether to read the company's site at all.
        render_budget: The run's allowance of browser visits. ``None`` means
            the browser is never used, which is the default: a render costs
            seconds where a fetch costs milliseconds.

    Returns:
        What was established. Never raises: a company that cannot be resolved
        yields an unresolved record and the pass moves on.
    """
    started = time.monotonic()
    key = str(record.get("company_key") or "")
    name = str(record.get("company") or record.get("company_name") or "") or key
    previous = str(record.get("it_link") or "").strip()

    def finish(**kwargs) -> Enrichment:
        """Stamp the shared fields onto a result."""
        return Enrichment(
            company_key=key,
            company=name,
            previous_it_link=previous,
            row=int(record.get("row") or 0),
            seconds=time.monotonic() - started,
            **kwargs,
        )

    # --- rule 5: a stored board is authoritative, and costs nothing --------
    if previous:
        platform = detect_platform(previous)
        # UNKNOWN means the cell is filler rather than a URL; anything else
        # parsed cleanly and is the operator's answer.
        stored = normalise_url(previous) if platform is not Platform.UNKNOWN else ""

        if stored and is_ats(platform):
            return finish(
                it_link=stored,
                platform=platform,
                status=STATUS_KEPT,
                reason=f"already names a {platform.value} board",
            )

        if stored:
            # A working URL that names no vendor is still the operator's
            # answer. Replacing it would be second-guessing, not repairing.
            return finish(
                it_link=stored,
                platform=platform,
                status=STATUS_KEPT,
                reason="stored IT Link is a URL but names no known vendor; left as is",
            )

        logger.debug("{}: stored IT Link {!r} is not a usable URL", name, previous)

    # --- rules 3 and 6: the existing chain, then one hop further -----------
    if not discover or session is None:
        return finish(status=STATUS_UNRESOLVED, reason="discovery is switched off")

    if not (record.get("website") or record.get("career_url")):
        return finish(
            status=STATUS_UNRESOLVED,
            reason="no website or careers page to search from",
        )

    try:
        resolution = resolve_company(record, session=session, discover=True)
    except Exception as exc:  # noqa: BLE001 - one company must not end the pass
        logger.opt(exception=True).debug("Resolution raised for {}", name)
        return finish(status=STATUS_BLOCKED, reason=f"resolution failed: {exc}"[:120])

    board = normalise_url(resolution.it_link)
    platform = detect_platform(board) if board else Platform.UNKNOWN

    # The chain landed on a careers page rather than a board. That is a fine
    # crawl seed and a useless IT Link, so keep looking.
    if not (board and is_ats(platform)):
        found = _hunt_for_board(
            record, resolution.career_url, session, render_budget=render_budget
        )
        if found:
            board = normalise_url(found)
            platform = detect_platform(board)

    # A vendor was named. Three things can still make the URL unfit to store,
    # and each is refused with its own reason rather than quietly dropped.
    if board and is_ats(platform):
        if not _is_usable_board(board, platform):
            refusal = f"found {platform.value}, but the link is its sign-in app, not a board"
        elif platform in AGGREGATORS:
            refusal = f"{platform.value} lists the jobs but is not the company's own ATS"
        elif _names_one_posting(board):
            refusal = f"found {platform.value}, but the link names one posting, not a board"
        else:
            refusal = ""

        if refusal:
            return finish(
                it_link=board,
                platform=platform,
                status=STATUS_REJECTED,
                reason=refusal,
                career_url=resolution.career_url,
            )

    if board and is_ats(platform):
        status = STATUS_REPLACED if previous else STATUS_DISCOVERED
        reason = (
            f"stored value {previous!r} is not a usable URL; replaced with a "
            f"{platform.value} board"
            if previous
            else f"{platform.value} board found via {resolution.source}"
        )
        return finish(
            it_link=board,
            platform=platform,
            status=status,
            reason=reason,
            career_url=resolution.career_url,
        )

    # --- rule 7: nothing confident, so nothing written --------------------
    # A page that could not be read and a page that simply names no vendor are
    # different outcomes. Conflating them would report a site that refused us
    # as a company that has no applicant tracking system.
    block = _blocker_for(resolution.career_url or str(record.get("website") or ""), session)
    if block is not Block.NONE:
        return finish(
            status=STATUS_BLOCKED,
            reason=block.value,
            career_url=resolution.career_url,
        )

    if not resolution.resolved:
        return finish(
            status=STATUS_UNRESOLVED,
            reason=resolution.detail or "no careers page or board could be found",
            career_url=resolution.career_url,
        )

    return finish(
        status=STATUS_UNRESOLVED,
        reason="a careers page was found but it names no known ATS vendor",
        career_url=resolution.career_url,
    )


def _hunt_for_board(
    record: Mapping[str, str],
    career_url: str,
    session: object,
    render_budget: Optional["_Budget"] = None,
) -> str:
    """Keep looking for a vendor board after the plain resolution found none.

    :func:`~crawler.resolve.resolve_company` stops early in the shape this
    sheet is actually in. Its ``Website`` column is blank and its
    ``Career Page URL`` column holds the company's **home page** rather than a
    careers page, so resolution takes the "the row names a careers page" branch
    and scans that one page for an outbound vendor link. A company whose header
    links ``Careers`` to an internal page — which is most of them — yields
    nothing, and the richer :func:`~crawler.career_finder.find_careers_url`
    chain never runs, because that one keys off ``Website``.

    So this runs it explicitly against whichever URL the row does have, and
    then follows the page it returns one hop for a vendor link.

    Args:
        record: The company record.
        career_url: Whatever resolution settled on as the careers page.
        session: HTTP session to use.

    Returns:
        A vendor board URL, or ``""``. Never raises.
    """
    from crawler.career_finder import find_careers_url

    root = normalise_url(str(record.get("website") or "")) or normalise_url(career_url)
    if not root:
        return ""

    try:
        found = find_careers_url(root, session)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - discovery is best effort
        logger.opt(exception=True).debug("Careers-page search failed for {}", root)
        return ""

    if found and is_ats(detect_platform(found)):
        return found

    # `found` is a careers page on the company's own domain. The board is
    # usually on it -- embedded far more often than linked, which is what
    # `_board_on` reads and `ats_link_on` cannot.
    for page in (found, career_url, root):
        if not page:
            continue
        board = _board_on(page, session)
        if board:
            return board

    # Nothing on the site says where the jobs are. Try where boards
    # conventionally live before giving up.
    probed, readable = _probe_for_board(root, session)
    if probed:
        return probed

    # Still nothing, and the board may simply not exist until its JavaScript
    # has run. The browser settles it, and the budget stops that costing hours.
    if render_budget is None:
        return ""

    # Spend the visits on pages that answered and named no board -- a real
    # careers page whose board is client-side. `found` comes first because
    # career_finder scored it as the best careers page on the site.
    queue: List[str] = []
    for page in [found, *readable, career_url]:
        if page and page not in queue:
            queue.append(page)

    visited = 0
    while queue and visited < _RENDER_TARGETS:
        page = queue.pop(0)
        if not render_budget.claim():
            return ""
        visited += 1

        board, deeper = _board_in_rendered(page)
        if board:
            return board

        # The board is often one hop below the careers page, and only the
        # rendered DOM names that hop. Queue the most specific one.
        for candidate in deeper[:1]:
            if candidate not in queue:
                queue.insert(0, candidate)

    return ""


def _looks_like_an_asset(url: str) -> bool:
    """Whether a URL is a vendor's static file rather than its board.

    A widget's stylesheet and its bootstrap script both live on the vendor's
    own domain, so both detect as the vendor. Neither is a board, and neither
    names the tenant: ``static.smartrecruiters.com/job-widget/1.5.5/…`` is
    identical for every SmartRecruiters customer.

    Args:
        url: The candidate URL.

    Returns:
        ``True`` when the URL is an asset.
    """
    path = (urlsplit(url).path or "").lower()
    if path.endswith(_ASSET_SUFFIXES):
        return True
    return any(fragment in path for fragment in _ASSET_PATHS)


def _canonical_board(url: str, platform: Platform) -> str:
    """Turn an embed URL into the board it embeds, where that is derivable.

    Several vendors publish a board through a script or iframe whose URL names
    the tenant. Those are recoverable and worth recovering, because the embed
    URL itself is JavaScript and an adapter pointed at it reads nothing::

        boards.greenhouse.io/embed/job_board/js?for=acme -> job-boards.greenhouse.io/acme
        acme.bamboohr.com/js/embed.js                    -> acme.bamboohr.com/careers
        recruitingbypaycor.com/career/iframe.action?…    -> …/career/CareerHome.action?…

    Where the tenant is *not* in the URL — a shared widget bundle — nothing is
    returned rather than a guess, and the caller treats it as no answer.

    Args:
        url: The URL found on the page.
        platform: The vendor detected for it.

    Returns:
        A board URL, or ``""`` when this URL cannot yield one.
    """
    split = urlsplit(url)
    host = (split.hostname or "").lower()
    query = parse_qs(split.query)

    if platform is Platform.GREENHOUSE and "/embed/job_board" in (split.path or "").lower():
        slug = (query.get("for") or [""])[0].strip()
        return f"https://job-boards.greenhouse.io/{slug}" if slug else ""

    if platform is Platform.BAMBOOHR and host.endswith("bamboohr.com"):
        # The tenant is the hostname, so any asset on it still identifies the
        # board -- unlike a shared CDN, every tenant has its own subdomain.
        tenant = host.split(".")[0]
        return f"https://{host}/careers" if tenant not in ("www", "static") else ""

    if platform is Platform.PAYCOR and "iframe.action" in (split.path or "").lower():
        client = (query.get("clientId") or query.get("clientid") or [""])[0].strip()
        base = f"{split.scheme or 'https'}://{host}/career/CareerHome.action"
        return f"{base}?clientId={client}" if client else ""

    # From here the tenant is read out of the path, so an asset has to be
    # refused *first*: `static.smartrecruiters.com/a.css` would otherwise
    # yield a tenant called "a.css". The vendors above are exempt because
    # their tenant is in the hostname, which an asset path cannot corrupt.
    if _looks_like_an_asset(url):
        return ""

    if platform is Platform.SMARTRECRUITERS:
        # A rendered board publishes its postings, not its board: KIPP's page
        # carries `www.smartrecruiters.com/KIPP/7440001453-some-job` and links
        # to the board itself nowhere. The tenant is the first path segment --
        # but only on the customer-facing hosts. An API host puts its own
        # route there, and `api.smartrecruiters.com/job-api/...` would
        # otherwise yield a tenant called "job-api".
        if host.split(".")[0] not in ("www", "careers", "jobs"):
            return ""
        segments = [part for part in (split.path or "").split("/") if part.strip()]
        if len(segments) < 2:
            return ""
        tenant = _first_segment(split.path)
        return f"https://careers.smartrecruiters.com/{tenant}" if tenant else ""

    if platform is Platform.JOBVITE and host.startswith("jobs."):
        tenant = _first_segment(split.path)
        return f"https://jobs.jobvite.com/{tenant}" if tenant else ""

    # Not an embed shape this knows, and not an asset. Taken at face value.
    return url


def _first_segment(path: str) -> str:
    """The first meaningful path segment, or ``""``.

    Args:
        path: A URL path.

    Returns:
        The segment, unless it is one of the vendor-side words that appears
        where a tenant would and is not one.
    """
    segments = [part for part in (path or "").split("/") if part.strip()]
    if not segments:
        return ""
    first = segments[0].strip()
    return "" if first.lower() in _NOT_A_TENANT else first


def _board_candidates(markup: str, page_url: str) -> List[str]:
    """Every URL on a page that might be a board, in order of trustworthiness.

    ``crawler.resolve.ats_link_on`` reads ``<a href>`` and nothing else, which
    is why most of this sheet's boards went undetected: they are embedded, not
    linked. This reads the five other places a board announces itself, and
    keeps anchors first because a real link is better evidence than a widget.

    Args:
        markup: The page's HTML.
        page_url: Its URL, for resolving relative references.

    Returns:
        Absolute URLs, de-duplicated, anchors first.
    """
    try:
        soup = parse_html(markup)
    except Exception:  # noqa: BLE001 - malformed markup is not fatal
        return []

    ordered: List[str] = []

    def add(value: object) -> None:
        """Resolve and keep a reference, ignoring anything unusable."""
        resolved = absolute_url(page_url, str(value or ""))
        if resolved and resolved not in ordered:
            ordered.append(resolved)

    for tag, attribute in (
        ("a", "href"), ("iframe", "src"), ("script", "src"),
        ("form", "action"), ("link", "href"),
    ):
        for node in soup.find_all(tag)[:_MAX_LINKS]:
            value = node.get(attribute)
            if value:
                add(value)

    for node in soup.find_all("script"):
        if node.get("src"):
            continue
        for found in _URL_IN_SCRIPT.findall(node.get_text() or "")[:_MAX_LINKS]:
            add(found.rstrip("\\\"';,)"))

    return ordered


def _board_on(page_url: str, session: object, remember: bool = False):
    """Read one page and return the first usable vendor board on it.

    Args:
        page_url: The page to read.
        session: HTTP session to use.
        remember: When set, also report whether the page answered at all, so a
            caller can tell "no board here" from "nothing here".

    Returns:
        A board URL, or ``""``. With ``remember``, ``(board, answered)``.
        Never raises.
    """
    def answer(board: str, answered: bool = False):
        """Shape the result the caller asked for."""
        return (board, answered) if remember else board

    if not page_url or session is None:
        return answer("")

    try:
        markup = get_text(session, page_url)
    except Exception:  # noqa: BLE001 - discovery is best effort
        logger.debug("Could not read {} while hunting for a board", page_url)
        return answer("")

    if not markup:
        return answer("")

    for candidate in _board_candidates(markup, page_url):
        platform = detect_platform(candidate)
        if not is_ats(platform):
            continue

        board = _canonical_board(candidate, platform)
        if not board:
            continue

        # Re-detect: a derived URL must still name the vendor it came from.
        if not is_ats(detect_platform(board)):
            continue

        logger.info("Board for {} found via {}", page_url, board)
        return answer(board, True)

    return answer("", True)


class _Budget:
    """A run's allowance of browser visits, claimed rather than checked.

    Claiming under a lock is what stops a pool of workers all deciding at once
    that there is one visit left. This is the same reasoning — and the same
    shape — as :meth:`crawler.weekly_run.WeeklyRun._claim_render`, kept
    separate because the two budgets are spent on different things and an
    operator should be able to set them independently.

    Args:
        total: How many visits the whole run may make. ``0`` disables the
            browser entirely, which is the default.
    """

    def __init__(self, total: int = 0) -> None:
        self._left = max(0, int(total))
        self._lock = threading.Lock()
        self.spent = 0

    def claim(self) -> bool:
        """Take one visit from the allowance.

        Returns:
            ``True`` when a visit was claimed, ``False`` when it is spent.
        """
        with self._lock:
            if self._left <= 0:
                return False
            self._left -= 1
            self.spent += 1
            return True

    @property
    def remaining(self) -> int:
        """Visits still available."""
        return self._left


def _board_in_rendered(page_url: str) -> str:
    """Visit one page in a browser and read the board out of what it becomes.

    Two sources, because boards hide in both. The rendered DOM carries what a
    visitor sees — PACE Supply's iCIMS portal is written into the page only
    after its JavaScript runs. The network log carries what the page *fetched*,
    which is where Hammerspace's Rippling board appears: requested, its results
    painted into a div, its URL never written into the document at all.

    Args:
        page_url: The page to visit.

    Returns:
        ``(board, next_pages)`` — the board when one was found, and otherwise
        the same-site careers links the rendered page revealed, because on
        several of these sites the board is one hop *below* the careers page
        and only the rendered DOM names that hop. Never raises: the browser is
        best effort and a company that cannot be rendered is not resolved.
    """
    try:
        page = render_page(page_url, capture_network=True)
    except Exception:  # noqa: BLE001 - the browser must not end a run
        logger.opt(exception=True).debug("Render failed for {}", page_url)
        return "", []

    if page is None or not getattr(page, "ok", False):
        logger.debug("Render produced nothing for {}", page_url)
        return "", []

    # The DOM first: a link a visitor could click is better evidence than a
    # background request, which may be telemetry that merely mentions a vendor.
    candidates = _board_candidates(page.html or "", page.url or page_url)
    candidates += [str(url) for url in getattr(page, "requests", []) or []]

    for candidate in candidates:
        platform = detect_platform(candidate)
        if not is_ats(platform):
            continue

        board = _canonical_board(candidate, platform)
        if not board or not is_ats(detect_platform(board)):
            continue

        logger.info("Board for {} found in the browser: {}", page_url, board)
        return board, []

    return "", _careers_links_on(page.html or "", page.url or page_url)


def _careers_links_on(markup: str, page_url: str) -> List[str]:
    """Same-site links whose wording says careers, for one further hop.

    Cushing Terrell's board is on ``/joinus/job-listings/`` and KIPP's on
    ``/careers/apply-now/`` — a level below the page a careers search lands on,
    and named only once the page has rendered.

    Args:
        markup: The rendered DOM.
        page_url: The page it came from.

    Returns:
        Absolute same-site URLs, most specific first.
    """
    from crawler.career_finder import CAREER_WORDS

    try:
        soup = parse_html(markup)
    except Exception:  # noqa: BLE001
        return []

    own = (urlsplit(page_url).hostname or "").lower()
    found: List[str] = []
    for node in soup.find_all("a", href=True)[:_MAX_LINKS]:
        url = absolute_url(page_url, node["href"])
        if not url or url.rstrip("/") == page_url.rstrip("/"):
            continue
        host = (urlsplit(url).hostname or "").lower()
        if host and not (host.endswith(own) or own.endswith(host)):
            continue
        blob = f"{(node.get_text() or '').strip().lower()} {url.lower()}"
        if any(word in blob for word in CAREER_WORDS) and url not in found:
            found.append(url)

    # Deepest first: a board lives below a careers page, never above one.
    return sorted(found, key=lambda url: -url.count("/"))


def _probe_for_board(base: str, session: object) -> str:
    """Try the conventional places a board lives on a company's own domain.

    Reached only when nothing on the site links or embeds one. Bounded by
    :data:`MAX_PROBES` and stops at the first answer, so a company with no
    board costs a fixed handful of requests rather than one per candidate.

    Args:
        base: Any URL on the company's domain.
        session: HTTP session to use.

    Returns:
        ``(board, readable)`` — the board when one was found, and the candidate
        URLs that answered with real markup. A page that reads fine and names
        no board is the best thing to spend a browser visit on.
    """
    split = urlsplit(normalise_url(base))
    host = (split.hostname or "").lower()
    if not host:
        return "", []

    scheme = split.scheme or "https"
    root = host[4:] if host.startswith("www.") else host

    paths = [f"{scheme}://{host}{path}" for path in _CAREERS_PATHS]
    subdomains = [f"{scheme}://{sub}.{root}" for sub in _CAREERS_SUBDOMAINS]

    # Interleaved rather than paths-then-subdomains, because MAX_PROBES would
    # otherwise be spent entirely on paths and the subdomains never tried --
    # and a `jobs.` subdomain is where Georgetown's and San Diego State's
    # boards actually live.
    candidates: List[str] = []
    for pair in zip_longest(paths, subdomains):
        candidates.extend(url for url in pair if url)

    readable: List[str] = []
    for candidate in candidates[:MAX_PROBES]:
        board, answered = _board_on(candidate, session, remember=True)
        if board:
            return board, readable
        if answered:
            readable.append(candidate)

    return "", readable


def _names_one_posting(url: str) -> bool:
    """Whether a URL points at a single job rather than at a board.

    A careers page often links to one currently-open role rather than to the
    board itself. The link carries the vendor's hostname, so the vendor is
    identified correctly and the URL is still the wrong thing to store: pointed
    at it, an adapter derives a search URL for a posting instead of a board.
    Providence's ``providence.avature.net/...?jobId=12086`` is the case that
    put this here — its derived search URL 404s.

    The test reuses :data:`crawler.identity.ID_QUERY_PARAMETERS`, which already
    names the parameters that carry a posting's identity, rather than inventing
    a second list that could disagree with it.

    Only query parameters are examined, deliberately. Several real boards carry
    a UUID in their path — UltiPro's ``/JobBoard/<uuid>/`` is a board, not a
    posting — so a broader test would reject them.

    Args:
        url: The candidate board URL.

    Returns:
        ``True`` when the URL identifies one posting.
    """
    from crawler.identity import ID_QUERY_PARAMETERS

    for name, value in parse_qsl(urlsplit(url).query):
        if name.strip().lower() in ID_QUERY_PARAMETERS and value.strip():
            return True
    return False


def _is_usable_board(url: str, platform: Platform) -> bool:
    """Whether a detected URL is a board an adapter could actually read.

    Detecting the vendor is not the same as having found its job board: a
    careers page can deep-link into the vendor's signed-in application, which
    carries the vendor's own hostname and is useless to the crawler.

    Args:
        url: The candidate board URL.
        platform: The vendor detected for it.

    Returns:
        ``True`` unless the URL is on a host known to serve the vendor's
        application rather than its public board.
    """
    host = urlsplit(url).hostname or ""
    host = host.lower().lstrip(".")

    for vendor, application_host in _APPLICATION_HOSTS:
        if platform is not vendor:
            continue
        if host == application_host or host.endswith(f".{application_host}"):
            return False

    return True


def _blocker_for(url: str, session: object) -> Block:
    """Name why a site could not be read, when that is why nothing was found.

    Args:
        url: The page that could not be read.
        session: HTTP session to use.

    Returns:
        The blocker, or :attr:`Block.NONE` when the page reads fine and the
        problem is simply that it names no board.
    """
    if not url or session is None:
        return Block.NONE
    try:
        get_text(session, url)
    except Exception as exc:  # noqa: BLE001 - classification is best effort
        return classify_text(str(exc))
    return Block.NONE


def discover_missing_boards(
    records: Sequence[Mapping[str, str]],
    session_factory: Optional[Callable[[], Any]] = None,
    workers: int = DEFAULT_WORKERS,
    limit: int = 0,
    render_budget: int = 0,
) -> DiscoveryReport:
    """Find boards for every company that names none.

    Args:
        records: Company records, as
            :meth:`sheets.companies.CompanyRepository.prepare_roster` returns.
        session_factory: Builds the HTTP session each worker uses. Injected so
            a test can supply a fake and never reach the network.
        workers: Companies examined at once.
        limit: Examine only the first N that need a board. ``0`` means all.
        render_budget: Browser visits the whole pass may make, shared across
            workers. ``0``, the default, never opens a browser.

    Returns:
        What was found. Writes nothing anywhere — persisting the result is the
        caller's decision, and a separate one.
    """
    report = DiscoveryReport()
    started = time.monotonic()

    pending = []
    for record in records:
        if needs_a_board(record):
            pending.append(record)
        else:
            report.skipped_have_board += 1

    if limit > 0:
        pending = pending[:limit]

    report.examined = len(pending)
    if not pending:
        report.seconds = time.monotonic() - started
        return report

    build = session_factory or (lambda: build_session(retries=SETTINGS.retries))
    budget = _Budget(render_budget) if render_budget > 0 else None

    def examine(record: Mapping[str, str]) -> Enrichment:
        """Look at one company, treating any failure as a blocked result."""
        session = build()
        try:
            return discover_board(
                record, session=session, discover=True, render_budget=budget
            )
        except Exception as exc:  # noqa: BLE001 - one company must not end the pass
            logger.opt(exception=True).debug(
                "Discovery raised for {}", record.get("company")
            )
            return Enrichment(
                company_key=str(record.get("company_key") or ""),
                company=str(record.get("company") or ""),
                previous_it_link=str(record.get("it_link") or ""),
                row=int(record.get("row") or 0),
                status=STATUS_BLOCKED,
                reason=f"discovery raised: {exc}"[:120],
            )
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    count = max(1, min(int(workers), len(pending)))
    if count == 1:
        report.entries = [examine(record) for record in pending]
    else:
        with ThreadPoolExecutor(max_workers=count) as pool:
            report.entries = list(pool.map(examine, pending))

    report.seconds = time.monotonic() - started
    report.renders_spent = budget.spent if budget else 0
    return report


def roster_with_rows(repository: Any) -> List[Dict[str, str]]:
    """Read the company list, keeping the sheet row each record came from.

    :meth:`sheets.companies.CompanyRepository.prepare_roster` is the usual way
    in, and it drops the row number and offers to write derived keys back. This
    stage needs the row (it addresses its writes by position, so it can never
    append a duplicate) and must never write a ``Company Key``, so it reads the
    store directly instead.

    Args:
        repository: A :class:`~sheets.companies.CompanyRepository`.

    Records are de-duplicated by company key, exactly as
    :meth:`~sheets.companies.CompanyRepository.prepare_roster` does, so this
    stage examines the same population the weekly crawl does — 65 companies
    across 109 rows on the live sheet, not 109.

    **The row kept for a company is one that already names a board, if any.**
    That is what makes "an existing board costs no discovery request" hold even
    when a company appears several times: choosing a blank duplicate instead
    would send the crawler off to rediscover a board the sheet already knows,
    and could write a second, different URL for one company.

    Args:
        repository: A :class:`~sheets.companies.CompanyRepository`.

    Returns:
        One record per distinct company, each carrying the ``row`` it came
        from.
    """
    from utils.names import company_key as derive_company_key

    chosen: Dict[str, Dict[str, str]] = {}
    for company in repository.store.read():
        name = company.get("company_name")
        if not name:
            continue

        # Derived only so discovery has something to key on; it is never
        # written back, because Company Key is not this stage's to set.
        key = company.get("company_key") or derive_company_key(
            name,
            company.get("website"),
            company.get("career_url") or company.get("it_link"),
        )
        if not key:
            continue

        record = {
            "company": name,
            "website": company.get("website"),
            "career_url": company.get("career_url"),
            "it_link": company.get("it_link"),
            "company_key": key,
            "row": company.row,
        }

        seen = chosen.get(key)
        if seen is None or (needs_a_board(seen) and not needs_a_board(record)):
            chosen[key] = record

    return list(chosen.values())


def apply_discoveries(
    repository: Any,
    report: DiscoveryReport,
    dry_run: bool = False,
) -> Any:
    """Write the discovered boards into ``MASTER_COMPANIES``.

    Writes are addressed **by row number**, not by key. That is what makes a
    duplicate ``Company Key`` impossible: :meth:`sheets.storage.Tab.update_rows`
    can only overwrite rows that already exist, and has no path that appends.
    It also writes only the named fields, so every other cell in the row —
    ``Company Name``, ``Website``, ``Career Page URL``, ``Company Key`` and the
    rest — is left exactly as it was found.

    Args:
        repository: A :class:`~sheets.companies.CompanyRepository`.
        report: What discovery established.
        dry_run: Work out what would change, and write nothing.

    Returns:
        The store's :class:`~sheets.storage.UpsertResult`.
    """
    changes: Dict[int, Dict[str, str]] = {}
    for entry in report.entries:
        if not entry.storable:
            continue
        changes[entry.row] = entry.sheet_updates()

    if not changes:
        logger.info("Nothing to write: no board was both discovered and storable")

    return repository.store.update_rows(changes, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -m crawler.ats_discovery",
        description=(
            "Find the ATS board URL for every company in MASTER_COMPANIES that "
            "names none, and report what was found."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be filled in, and write nothing",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write the discovered boards into the IT Link and ATS / Platform "
            "columns. Touches no other column and no other row"
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Examine only the first N companies needing a board"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Companies examined at once (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--render",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Allow up to N browser visits across the whole pass for companies "
            "whose board only exists once JavaScript has run (default: 0, never)"
        ),
    )
    parser.add_argument(
        "--retries", type=int, default=2, help="HTTP attempts per request (default: 2)"
    )
    parser.add_argument(
        "--spreadsheet", default=None, help="Spreadsheet id or URL (default: from the environment)"
    )
    parser.add_argument("--json", default=None, help="Also write the report as JSON to this path")
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"),
        help="Console log level (default: WARNING)",
    )
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Report the boards that could be filled in.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` on success, ``2`` when the spreadsheet is not configured, ``1``
        on an API failure.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    logger.remove()
    logger.add(sys.stderr, level=args.log_level, format="<level>{level: <8}</level> | {message}")

    writing = bool(args.apply)

    from sheets.auth import CredentialsError, build_service, resolve_credentials
    from sheets.auth import resolve_spreadsheet_id
    from sheets.client import SheetsClient
    from sheets.companies import CompanyRepository

    configure(retries=max(1, args.retries), discover_careers=True)

    try:
        spreadsheet_id = resolve_spreadsheet_id(args.spreadsheet)
        # A dry run asks for the read-only scope, so its promise of writing
        # nothing is enforced by Google rather than merely intended here.
        credentials = resolve_credentials(read_only=not writing)
        service = build_service(read_only=not writing, credentials=credentials.credentials)
    except CredentialsError as exc:
        print(f"\nNot configured yet.\n\n{exc}\n", file=sys.stderr)
        return 2

    client = SheetsClient(service, spreadsheet_id)
    companies = CompanyRepository(client)

    try:
        # Read the store directly: this keeps the row number each record came
        # from, and never offers to write a derived Company Key back.
        records = roster_with_rows(companies)
    except Exception as exc:  # noqa: BLE001 - report Google's own wording
        print(f"\nCould not read the spreadsheet:\n  {exc}\n", file=sys.stderr)
        return 1

    report = discover_missing_boards(
        records,
        workers=max(1, args.workers),
        limit=max(0, args.limit),
        render_budget=max(0, args.render),
    )

    report.sheet_reads = getattr(client.stats, "reads", 0)
    report.sheet_writes = getattr(client.stats, "writes", 0)
    print(report.render(dry_run=not writing))

    planned = report.planned_updates()
    print("=" * 96)
    print("  EXACT CELLS THAT WOULD BE WRITTEN" if not writing else "  EXACT CELLS WRITTEN")
    print("=" * 96)
    for update in sorted(planned, key=lambda item: item["row"]):
        print(f"  row {update['row']:>4}  {update['company'][:28]:<29}")
        print(f"      IT Link         = {update['it_link'][:74]}")
        print(f"      ATS / Platform  = {update['platform']}")
    print(f"\n  {len(planned)} row(s), 2 cells each. No other column is touched.")

    result = None
    if writing and planned:
        try:
            result = apply_discoveries(companies, report, dry_run=False)
        except Exception as exc:  # noqa: BLE001 - report Google's own wording
            print(f"\nThe write failed:\n  {exc}\n", file=sys.stderr)
            return 1

    report.sheet_reads = getattr(client.stats, "reads", 0)
    report.sheet_writes = getattr(client.stats, "writes", 0)

    print("=" * 96)
    if writing:
        updated = getattr(result, "updated", 0)
        skipped = getattr(result, "skipped", 0)
        unchanged = getattr(result, "unchanged", 0)
        cells = getattr(result, "cells_written", 0)
        print(f"  rows updated {updated}   unchanged {unchanged}   skipped {skipped}")
        print(f"  cells written {cells}")
        print(f"  Sheets reads {report.sheet_reads}   writes {report.sheet_writes}")
        if skipped:
            print(f"  WARNING: {skipped} row(s) could not be addressed and were not written")
    else:
        print(f"  Nothing was written. Sheets reads {report.sheet_reads}, "
              f"writes {report.sheet_writes}.")
        print("  Re-run with --apply to store these.")
    print("=" * 96)

    if args.json:
        import json
        from pathlib import Path

        destination = Path(args.json)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "examined": report.examined,
            "skipped_have_board": report.skipped_have_board,
            "discovered": report.discovered,
            "platforms": report.platforms,
            "seconds": round(report.seconds, 2),
            "sheet_reads": report.sheet_reads,
            "sheet_writes": report.sheet_writes,
            "planned_updates": report.planned_updates(),
            "entries": [
                {
                    "company": entry.company,
                    "company_key": entry.company_key,
                    "status": entry.status,
                    "platform": entry.platform.value,
                    "it_link": entry.it_link,
                    "previous_it_link": entry.previous_it_link,
                    "career_url": entry.career_url,
                    "reason": entry.reason,
                    "seconds": round(entry.seconds, 2),
                }
                for entry in report.entries
            ],
        }
        destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nWrote {destination}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
