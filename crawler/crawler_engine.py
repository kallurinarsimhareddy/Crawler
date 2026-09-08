"""Drive the end-to-end crawl over every company in the input sheet.

The engine owns the pipeline and everything operational around it::

    read_companies -> pick a seed URL -> detect_platform -> adapter -> Job list

**Seed selection.** A company record carries up to three URLs, and they are not
equally useful. ``it_link`` points straight at the applicant tracking system;
``career_url`` is usually a marketing page; ``website`` is a last resort. They
are tried in that order, and the first one that resolves to a usable URL wins.
A candidate that is unusable — blank, or filler such as ``"N/A"`` — is skipped
rather than allowed to sink the company.

**Dispatch.** There is no branching on platform. A registry maps
:class:`~crawler.platform_detector.Platform` to the adapter that handles it, and
the engine only ever performs a dictionary lookup::

    Platform.WORKDAY    -> adapters.workday.fetch_jobs
    Platform.GREENHOUSE -> adapters.greenhouse.fetch_jobs
    Platform.LEVER      -> adapters.lever.fetch_jobs
    Platform.ICIMS      -> adapters.icims.fetch_jobs
    Platform.ULTIPRO    -> adapters.ultipro.fetch_jobs
    Platform.GENERIC_HTML -> adapters.generic.fetch_jobs

A platform with no registered adapter yields an empty list, never an error, so
the run continues across the long tail of systems that are detected but not yet
supported. The registry is built by :func:`build_registry`, which registers only
the adapters that actually expose ``fetch_jobs`` — an unimplemented adapter
module is simply absent rather than a crash at import time.

**Injection.** Both the registry and the HTTP session factory are constructor
arguments, so a test can supply fake adapters and never touch the network::

    >>> engine = CrawlerEngine(registry={Platform.WORKDAY: fake_fetch})
    >>> jobs = engine.crawl([{"company": "Acme", "it_link": "https://acme.wd1.myworkdayjobs.com/en-US/X"}])

**Isolation.** One company's failure is logged, recorded on its
:class:`CrawlResult`, and stepped over. Nothing an adapter raises can end the
run.
"""

from __future__ import annotations

import importlib
import queue
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Callable,
    Dict,
    Final,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)
from urllib.parse import urlsplit

from loguru import logger

from config.settings import SETTINGS
from crawler.platform_detector import Platform, detect_platform
from models.job import Job
from utils.http import AdapterUrlError

__all__ = [
    "ADAPTER_MODULES",
    "SEED_FIELDS",
    "CrawlResult",
    "CrawlerEngine",
    "JobFetcher",
    "Outcome",
    "SessionFactory",
    "build_registry",
]


class Outcome(str, Enum):
    """Why a company ended up where it did.

    Version 1 knew only "produced jobs" and "did not". That collapses four
    genuinely different situations into one failure file, and they need
    different work to fix: an empty board needs nothing, an unsupported
    platform needs an adapter, a technical failure needs a retry or a browser,
    and an unidentifiable URL needs a better input sheet. Each now goes to its
    own report.
    """

    #: Postings were found.
    JOBS = "jobs"

    #: The board was read successfully and is genuinely advertising nothing.
    NO_JOBS = "no open jobs"

    #: The platform was identified but no adapter handles it yet.
    UNSUPPORTED = "unsupported platform"

    #: The board could not be read — an error, a block, a timeout.
    TECHNICAL = "technical failure"

    #: No usable URL, or nothing recognisable behind the one given.
    UNKNOWN = "unknown platform"


class JobFetcher(Protocol):
    """The contract every adapter's ``fetch_jobs`` satisfies.

    Adapters that need no HTTP session still accept the ``session`` keyword and
    ignore it, so the engine can pass one uniformly.
    """

    def __call__(
        self,
        career_url: str,
        company_name: str,
        session: Optional[object] = None,
    ) -> List[Job]:
        """Return every posting the platform advertises for this company."""
        ...


#: Builds the HTTP session shared across a run. Injected so tests can pass a
#: fake and callers can control retry and proxy policy in one place.
SessionFactory = Callable[[], object]

#: Platform -> module that adapts it. Listed here rather than imported eagerly
#: so that an unwritten adapter costs nothing.
ADAPTER_MODULES: Final[Dict[Platform, str]] = {
    Platform.WORKDAY: "adapters.workday",
    Platform.GREENHOUSE: "adapters.greenhouse",
    Platform.LEVER: "adapters.lever",
    Platform.ASHBY: "adapters.ashby",
    Platform.ICIMS: "adapters.icims",
    Platform.ULTIPRO: "adapters.ultipro",
    Platform.UKG: "adapters.ultipro",
    Platform.SMARTRECRUITERS: "adapters.smartrecruiters",
    Platform.SUCCESSFACTORS: "adapters.successfactors",
    Platform.ORACLE: "adapters.oracle",
    Platform.TALEO: "adapters.taleo",
    Platform.JOBVITE: "adapters.jobvite",
    Platform.TEAMTAILOR: "adapters.teamtailor",
    Platform.BAMBOOHR: "adapters.bamboohr",
    Platform.RECRUITEE: "adapters.recruitee",
    Platform.WORKABLE: "adapters.workable",
    Platform.DAYFORCE: "adapters.dayforce",
    Platform.ADP: "adapters.adp",
    # --- Added in version 2 -------------------------------------------------
    Platform.ADP_RM: "adapters.adp_rm",
    Platform.CORNERSTONE: "adapters.cornerstone",
    Platform.EIGHTFOLD: "adapters.eightfold",
    Platform.PHENOM: "adapters.phenom",
    Platform.PEOPLEADMIN: "adapters.peopleadmin",
    Platform.PAYLOCITY: "adapters.paylocity",
    Platform.PAYCOM: "adapters.paycom",
    Platform.PAYCOR: "adapters.paycor",
    Platform.UKG_READY: "adapters.ukg_ready",
    Platform.ISOLVED: "adapters.isolved",
    Platform.ASURE: "adapters.asure",
    Platform.JAZZHR: "adapters.jazzhr",
    Platform.RIPPLING: "adapters.rippling",
    Platform.PERSONIO: "adapters.personio",
    Platform.AVATURE: "adapters.avature",
    Platform.BULLHORN: "adapters.bullhorn",
    Platform.BREEZYHR: "adapters.breezyhr",
    Platform.PINPOINT: "adapters.pinpoint",
    Platform.COMEET: "adapters.comeet",
    Platform.FOUNTAIN: "adapters.fountain",
    Platform.NEOGOV: "adapters.neogov",
    Platform.OLEEO: "adapters.oleeo",
    Platform.JOBSCORE: "adapters.jobscore",
    Platform.GOHIRE: "adapters.gohire",
    Platform.HOMERUN: "adapters.homerun",
    Platform.JOIN: "adapters.join",
    Platform.ZOHO_RECRUIT: "adapters.zoho_recruit",
    Platform.MANATAL: "adapters.manatal",
    Platform.GEM: "adapters.gem",
    Platform.TALENTREEF: "adapters.talentreef",
    Platform.APPLICANTPRO: "adapters.applicantpro",
    Platform.APPLICANTSTACK: "adapters.applicantstack",
    Platform.CLEARCOMPANY: "adapters.clearcompany",
    Platform.CAREERPLUG: "adapters.careerplug",
    Platform.HIREOLOGY: "adapters.hireology",
    Platform.HRMDIRECT: "adapters.hrmdirect",
    Platform.SILKROAD: "adapters.silkroad",
    Platform.RECRUITERBOX: "adapters.recruiterbox",
    Platform.RADANCY: "adapters.radancy",
    Platform.INDEED: "adapters.indeed",
    # The generic fallback stays last: it is what everything unrecognised gets.
    Platform.GENERIC_HTML: "adapters.generic",
}

#: Record keys to try as the crawl seed, best first. ``it_link`` is the sheet's
#: direct link to the ATS; ``career_url`` is typically a marketing page; the
#: bare ``website`` only helps once career-page discovery exists.
SEED_FIELDS: Final[Tuple[str, ...]] = ("it_link", "career_url", "website")

#: Platforms that mean "nothing to dispatch", as opposed to "no adapter yet".
_NON_DISPATCHABLE: Final[frozenset] = frozenset({Platform.UNKNOWN})

#: Wording an adapter uses when it rejects a URL *because a browser is needed*
#: rather than because the URL is not its platform's. Both raise
#: :class:`~utils.http.AdapterUrlError`, and only the first is worth rendering.
#: These are the same phrases :func:`main._reason_family` groups on.
_NEEDS_A_BROWSER: Final[re.Pattern[str]] = re.compile(
    r"browser-driven|client-side|needs a browser|bot challenge|human verification",
    re.IGNORECASE,
)

#: How long a worker waits for a browser slot before giving up on its rescue.
#:
#: Generous, because waiting is the point: a worker that cannot get a slot must
#: not proceed without one. Bounded, because a render that wedges would
#: otherwise hold the last slot for the rest of the run, and this crawler has
#: been observed looping on anti-bot interstitials. Giving up yields the same
#: outcome as a machine with no browser installed.
_BROWSER_SLOT_TIMEOUT: Final[float] = 300.0

#: URLs from the sheet tried per company before giving up on it. Two, because
#: the pattern worth catching is a wrong ``it_link`` masking a good
#: ``career_url``; going further mostly re-crawls the same marketing site under
#: a third spelling and doubles the run for nothing.
_MAX_SEED_ATTEMPTS: Final[int] = 2


@dataclass(frozen=True)
class CrawlResult:
    """The outcome of crawling one company.

    Attributes:
        company: Company as named in the input sheet.
        platform: Platform detected from the chosen seed URL.
        seed_url: URL the crawl was actually run against, or ``""`` if the
            record offered none that was usable.
        seed_field: Record key ``seed_url`` came from, e.g. ``"it_link"``.
        jobs: Postings found. Empty when the company failed, had no usable URL,
            or runs on a platform with no adapter.
        error: One-line reason the company produced nothing, or ``None`` on
            success. Present so a run can be triaged without re-reading logs.
        seconds: Wall-clock time the company took, for the coverage report.
        discovered: Whether ``seed_url`` came from career-page discovery rather
            than from the input sheet.
    """

    company: str
    platform: Platform
    seed_url: str = ""
    seed_field: str = ""
    jobs: List[Job] = field(default_factory=list)
    error: Optional[str] = None
    seconds: float = 0.0
    discovered: bool = False

    @property
    def ok(self) -> bool:
        """Whether the company was crawled without error."""
        return self.error is None

    @property
    def outcome(self) -> Outcome:
        """Which report this company belongs in.

        Derived rather than stored, so it can never disagree with the error and
        job list it is derived from.

        Returns:
            The classification.
        """
        if self.jobs:
            return Outcome.JOBS
        if self.error is None:
            return Outcome.NO_JOBS
        if "no adapter for" in self.error:
            return Outcome.UNSUPPORTED
        if self.platform is Platform.UNKNOWN:
            return Outcome.UNKNOWN
        return Outcome.TECHNICAL


def build_registry(
    modules: Optional[Mapping[Platform, str]] = None,
) -> Dict[Platform, JobFetcher]:
    """Build the platform-to-adapter registry by importing the adapter modules.

    Only adapters that actually expose a callable ``fetch_jobs`` are registered.
    A module that is missing, fails to import, or is still a docstring-only stub
    is logged and left out, which is what lets the engine run today against the
    adapters that exist while the rest are written.

    Args:
        modules: Platform -> module path. Defaults to :data:`ADAPTER_MODULES`.

    Returns:
        Platform -> the module's ``fetch_jobs``, for every adapter available.
    """
    registry: Dict[Platform, JobFetcher] = {}

    for platform, module_path in (modules or ADAPTER_MODULES).items():
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            logger.debug("No adapter for {}: {} could not be imported ({})", platform.value, module_path, exc)
            continue

        fetch_jobs = getattr(module, "fetch_jobs", None)
        if not callable(fetch_jobs):
            logger.debug("No adapter for {}: {} defines no fetch_jobs()", platform.value, module_path)
            continue

        registry[platform] = fetch_jobs

    logger.info(
        "Adapter registry: {} platform(s) supported ({})",
        len(registry),
        ", ".join(sorted(platform.value for platform in registry)) or "none",
    )
    return registry


class _HostThrottle:
    """Keeps concurrent workers from converging on one host.

    A sheet of a thousand companies is not a thousand hosts: a hundred and
    thirty of them are on ``myworkdayjobs.com`` and a hundred more on
    ``adp.com``. Running eight workers without this would mean eight
    simultaneous crawls of the same vendor, which is both rude and the fastest
    way to be rate-limited into failing those companies.

    The delay is per host, not global, so unrelated companies still run fully
    in parallel.

    Args:
        delay: Minimum seconds between two crawls of the same host. ``0``
            disables throttling entirely.
    """

    def __init__(self, delay: float) -> None:
        self._delay = max(0.0, float(delay))
        self._next: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        """Block until this URL's host may be crawled again.

        Args:
            url: The URL about to be crawled.
        """
        if self._delay <= 0 or not url:
            return

        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return
        if not host:
            return

        with self._lock:
            now = time.monotonic()
            earliest = self._next.get(host, 0.0)
            wait_for = max(0.0, earliest - now)
            self._next[host] = max(now, earliest) + self._delay

        if wait_for > 0:
            logger.debug("Throttle: waiting {:.2f}s for {}", wait_for, host)
            time.sleep(wait_for)


class _BrowserSlots:
    """Caps how many workers may be inside a browser rescue at once.

    The rescue is the expensive path: an adapter that failed hands the page to
    headless Chromium, which costs seconds and hundreds of megabytes where an
    HTTP read costs milliseconds. Nothing bounded how many workers could be
    doing that simultaneously -- :data:`~config.settings.Settings.browser_budget`
    was declared, documented and read by no code at all -- so on a stretch of
    roster where half the companies fail, every worker rendered at once.

    A worker that cannot get a slot **waits** rather than proceeding without
    one, because the alternative is the unbounded behaviour this exists to stop.
    It waits with a deadline, though: a render that wedges would otherwise hold
    the last slot for the rest of the run, and this crawler has already been
    observed looping on anti-bot interstitials. A worker that waits past the
    deadline skips its rescue, which is the same outcome as a machine with no
    browser installed -- a degraded read, not a failed run.

    Args:
        limit: Concurrent rescues allowed. ``0`` means no cap, which is the
            shipped default and the behaviour every run to date has had.
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self._semaphore = (
            threading.BoundedSemaphore(self.limit) if self.limit else None
        )
        self._lock = threading.Lock()

        #: Rescues in flight right now, and the most ever at once. The second is
        #: what a test asserts on: "never more than the budget" is a statement
        #: about the peak, not about any single moment a test happens to look.
        self.live = 0
        self.peak = 0

        #: Seconds spent waiting for a slot, and rescues skipped because the
        #: wait ran out. Both are reported, so a budget set too low shows up as
        #: a number rather than as a run that is mysteriously slower.
        self.waited = 0.0
        self.skipped = 0

    @contextmanager
    def hold(self, timeout: float = _BROWSER_SLOT_TIMEOUT) -> Iterator[bool]:
        """Take a slot for the duration of a block.

        Args:
            timeout: Seconds to wait for a slot before giving up.

        Yields:
            ``True`` when a slot was taken and the caller may render, ``False``
            when the wait ran out and it must not. The slot is released on the
            way out however the block ends, so an exception in a render cannot
            strand one.
        """
        if self._semaphore is None:
            # No cap: the ungoverned behaviour every run has had so far.
            yield True
            return

        started = time.monotonic()
        taken = self._semaphore.acquire(timeout=max(0.0, float(timeout)))
        waited = time.monotonic() - started

        with self._lock:
            self.waited += waited
            if taken:
                self.live += 1
                self.peak = max(self.peak, self.live)
            else:
                self.skipped += 1

        try:
            yield taken
        finally:
            if taken:
                with self._lock:
                    self.live -= 1
                self._semaphore.release()

    def describe(self) -> str:
        """One line for a run report.

        Returns:
            The budget and what it cost.
        """
        if not self.limit:
            return "browser rescues: unlimited"
        return (
            f"browser rescues: at most {self.limit} at once "
            f"(peak {self.peak}, {self.waited:.1f}s waiting, {self.skipped} skipped)"
        )


class CrawlerEngine:
    """Crawls company records into :class:`~models.job.Job` records.

    The engine is stateless between calls and safe to reuse. Nothing about it
    is hard-wired: pass a registry to change or fake the adapters, and a session
    factory to control the HTTP client they share.

    Args:
        registry: Platform -> adapter. Defaults to :func:`build_registry`.
        session_factory: Builds the session handed to every adapter. When
            omitted, each adapter is passed ``None`` and builds its own, which
            is correct but forgoes connection reuse across companies.
        browser_budget: Browser rescues allowed at once. ``None`` reads
            :data:`~config.settings.SETTINGS`, which is what a run does;
            passing a number is for tests that need a known cap without
            mutating a process-wide singleton.
    """

    def __init__(
        self,
        registry: Optional[Mapping[Platform, JobFetcher]] = None,
        session_factory: Optional[SessionFactory] = None,
        browser_budget: Optional[int] = None,
    ) -> None:
        self._registry: Dict[Platform, JobFetcher] = dict(
            registry if registry is not None else build_registry()
        )
        self._session_factory = session_factory
        self._throttle = _HostThrottle(SETTINGS.per_host_delay)
        self.browser_slots = _BrowserSlots(
            SETTINGS.browser_budget if browser_budget is None else browser_budget
        )

    @property
    def supported_platforms(self) -> Sequence[Platform]:
        """Platforms this engine has an adapter for."""
        return tuple(self._registry)

    def select_seed(self, record: Mapping[str, str]) -> Tuple[str, str, Platform]:
        """Choose which of a record's URLs to crawl, and identify its platform.

        Candidates are tried in :data:`SEED_FIELDS` order. The first one that
        detects as anything other than :attr:`Platform.UNKNOWN` is taken —
        skipping past blanks and filler such as ``"N/A"`` rather than letting a
        junk ``it_link`` mask a perfectly good ``career_url``.

        Args:
            record: A company record from :func:`crawler.csv_reader.read_companies`.

        Returns:
            ``(seed_url, seed_field, platform)``. If no candidate is usable,
            ``("", "", Platform.UNKNOWN)``.
        """
        candidates = self.seed_candidates(record)
        return candidates[0] if candidates else ("", "", Platform.UNKNOWN)

    @staticmethod
    def seed_candidates(record: Mapping[str, str]) -> List[Tuple[str, str, Platform]]:
        """List every usable URL a record offers, best first.

        The sheet's best guess is not always right: an ``it_link`` can point at
        a vendor's public job *aggregator* rather than the company's own board,
        or at a tenant that has since moved. When that happens the company
        usually still has a perfectly good ``career_url`` sitting unused, so
        the crawl needs to know about the alternatives rather than only the
        winner.

        Args:
            record: A company record from
                :func:`crawler.csv_reader.read_companies`.

        Returns:
            ``(seed_url, seed_field, platform)`` for each usable candidate, in
            :data:`SEED_FIELDS` order, with duplicates removed. Empty when the
            record offers nothing crawlable.
        """
        candidates: List[Tuple[str, str, Platform]] = []
        seen: set = set()

        for field_name in SEED_FIELDS:
            candidate = str(record.get(field_name) or "").strip()
            if not candidate:
                continue

            platform = detect_platform(candidate)
            if platform is Platform.UNKNOWN:
                logger.debug("Ignoring unusable {}: {!r}", field_name, candidate)
                continue

            key = candidate.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)

            candidates.append((candidate, field_name, platform))

        return candidates

    def crawl_company(
        self,
        record: Mapping[str, str],
        session: Optional[object] = None,
    ) -> CrawlResult:
        """Crawl one company.

        Never raises on account of the company: an adapter that fails is caught,
        logged with its traceback, and reported through
        :attr:`CrawlResult.error`.

        Args:
            record: A company record with ``company`` and at least one of
                ``it_link``, ``career_url`` or ``website``.
            session: HTTP session to hand the adapter. When ``None`` the adapter
                builds its own.

        Returns:
            The outcome, including any jobs found.
        """
        started = time.monotonic()

        company = str(record.get("company") or "").strip()
        if not company:
            logger.warning("Skipping record with no company name: {}", dict(record))
            return CrawlResult(
                company="", platform=Platform.UNKNOWN, error="record has no company name"
            )

        candidates = self.seed_candidates(record)

        if not candidates:
            found, field_name, platform = self._discover_seed(record, session)
            if found:
                candidates = [(found, field_name, platform)]

        if not candidates:
            logger.warning("{}: no usable URL in the input sheet", company)
            return CrawlResult(
                company=company,
                platform=Platform.UNKNOWN,
                error="no usable URL in the input sheet",
                seconds=time.monotonic() - started,
            )

        discovered = candidates[0][1] == "discovered"

        # Try the sheet's best URL first, then its alternatives. A company with
        # a stale or wrong `it_link` very often still has a usable `career_url`
        # underneath it, and version 1 never looked.
        result = None
        for index, (seed_url, seed_field, platform) in enumerate(candidates[:_MAX_SEED_ATTEMPTS]):
            attempt = self._dispatch(
                company,
                seed_url,
                seed_field,
                platform,
                session,
                discovered and index == 0,
                started,
            )
            if attempt.jobs:
                return attempt
            if result is None:
                # Report the first attempt's outcome unless a later one works,
                # since that is the URL the sheet actually nominated.
                result = attempt
            if index + 1 < min(len(candidates), _MAX_SEED_ATTEMPTS):
                logger.info(
                    "{}: {} gave nothing, trying {}",
                    company,
                    seed_field,
                    candidates[index + 1][1],
                )

        seed_url, seed_field, platform = candidates[0]

        # A marketing page that yielded nothing is the single largest source of
        # empty results, and the board it links to is usually one hop away. Try
        # that hop before writing the company off.
        if (
            SETTINGS.discover_careers
            and any(item[2] is Platform.GENERIC_HTML for item in candidates[:_MAX_SEED_ATTEMPTS])
            and not discovered
        ):
            better = self._discover_seed(record, session, exclude=seed_url)
            if better[0]:
                logger.info("{}: retrying via discovered {}", company, better[0])
                retried = self._dispatch(
                    company, better[0], better[1], better[2], session, True, started
                )
                if retried.jobs:
                    return retried

        self._maybe_record_diagnostics(result)
        return result

    def _dispatch(
        self,
        company: str,
        seed_url: str,
        seed_field: str,
        platform: Platform,
        session: Optional[object],
        discovered: bool,
        started: float,
    ) -> CrawlResult:
        """Hand one URL to its adapter and wrap the outcome in a result.

        Args:
            company: Company as named in the input sheet.
            seed_url: URL to crawl.
            seed_field: Record key it came from, or ``"discovered"``.
            platform: Platform detected from ``seed_url``.
            session: HTTP session to hand the adapter.
            discovered: Whether ``seed_url`` came from discovery.
            started: When the company's crawl began, for the elapsed time.

        Returns:
            The outcome. Never raises on account of the adapter.
        """
        if platform in _NON_DISPATCHABLE:  # pragma: no cover - select_seed excludes these
            return CrawlResult(
                company=company,
                platform=platform,
                seed_url=seed_url,
                seed_field=seed_field,
                error=f"platform {platform.value}",
                seconds=time.monotonic() - started,
                discovered=discovered,
            )

        fetch_jobs = self._registry.get(platform)
        if fetch_jobs is None:
            logger.info("{}: {} is detected but has no adapter yet, skipping", company, platform.value)
            return CrawlResult(
                company=company,
                platform=platform,
                seed_url=seed_url,
                seed_field=seed_field,
                error=f"no adapter for {platform.value}",
                seconds=time.monotonic() - started,
                discovered=discovered,
            )

        logger.info("{}: crawling {} via {} ({})", company, platform.value, seed_field, seed_url)
        self._throttle.wait(seed_url)

        try:
            jobs = list(fetch_jobs(seed_url, company, session=session) or [])
        except Exception as exc:  # noqa: BLE001 - one company must not end the run
            logger.opt(exception=True).error(
                "{}: {} adapter failed on {}", company, platform.value, seed_url
            )

            rescued = self._rescue_with_browser(company, seed_url, platform, exc)
            if rescued:
                return CrawlResult(
                    company=company,
                    platform=platform,
                    seed_url=seed_url,
                    seed_field=seed_field,
                    jobs=rescued,
                    seconds=time.monotonic() - started,
                    discovered=discovered,
                )

            return CrawlResult(
                company=company,
                platform=platform,
                seed_url=seed_url,
                seed_field=seed_field,
                error=f"{type(exc).__name__}: {exc}",
                seconds=time.monotonic() - started,
                discovered=discovered,
            )

        logger.debug("{}: {} job(s) from {}", company, len(jobs), platform.value)
        return CrawlResult(
            company=company,
            platform=platform,
            seed_url=seed_url,
            seed_field=seed_field,
            jobs=jobs,
            seconds=time.monotonic() - started,
            discovered=discovered,
        )

    def _rescue_with_browser(
        self,
        company: str,
        seed_url: str,
        platform: Platform,
        failure: Optional[BaseException] = None,
    ) -> List[Job]:
        """Retry a failed board in a real browser before writing it off.

        An adapter fails for one of a handful of reasons — an anti-bot
        interstitial that only clears once JavaScript runs, a portal that
        renders client-side, a Cloudflare challenge, a vendor that changed
        shape — and a real browser answers all of them the same way. Doing it
        here rather than in each adapter means every platform gets the rescue,
        including the ones written before the browser existed, and no adapter
        has to know the browser is there.

        The rescue only runs on the failure path, so a board that reads cleanly
        over HTTP never pays for it — and it is skipped entirely when the
        adapter's verdict was that the *URL is not this company's board*. A
        browser cannot fix a wrong address, and rendering one anyway is how a
        vendor's public job aggregator ends up filed under whichever company
        the sheet happened to paste it against.

        Args:
            company: Company as named in the input sheet.
            seed_url: The URL that failed.
            platform: Platform detected for it, whose label the rescued jobs
                keep — the board is still a Workday board even when it took a
                browser to read.
            failure: What the adapter raised, used to tell "could not read this
                board" from "this is not that board".

        Returns:
            Any postings the browser found. Empty when the run forbids the
            browser, none is installed, the URL was rejected outright, or the
            page genuinely has none.
        """
        if not SETTINGS.browser_fallback:
            return []

        if isinstance(failure, AdapterUrlError) and not _NEEDS_A_BROWSER.search(str(failure)):
            logger.debug(
                "{}: not rescuing {} — the adapter rejected the URL itself", company, seed_url
            )
            return []

        # Imported here so a browserless run never pulls in the module.
        from adapters.generic import render_and_extract

        # The slot is taken *before* Chromium is asked for and given back on the
        # way out however this ends, so a render that raises cannot strand one.
        with self.browser_slots.hold() as slot:
            if not slot:
                logger.warning(
                    "{}: skipping the browser rescue on {} — no slot free within "
                    "{:.0f}s and the budget is {}. The board is reported as it "
                    "read over HTTP.",
                    company,
                    seed_url,
                    _BROWSER_SLOT_TIMEOUT,
                    self.browser_slots.limit,
                )
                return []

            try:
                jobs = render_and_extract(
                    seed_url, company, platform.value, career_page_url=seed_url
                )
            except Exception:  # noqa: BLE001 - the rescue must never end the run
                logger.opt(exception=True).debug(
                    "{}: browser rescue failed on {}", company, seed_url
                )
                return []

        if jobs:
            logger.success(
                "{}: {} job(s) rescued from {} by the browser", company, len(jobs), platform.value
            )
        return jobs

    def _discover_seed(
        self,
        record: Mapping[str, str],
        session: Optional[object],
        exclude: str = "",
    ) -> Tuple[str, str, Platform]:
        """Search the company's website for a careers page.

        Args:
            record: The company record.
            session: HTTP session to use. Discovery is skipped without one,
                since it would otherwise open a connection per company.
            exclude: A URL already tried, so discovery does not return it again.

        Returns:
            ``(seed_url, "discovered", platform)``, or ``("", "", UNKNOWN)``.
        """
        if not SETTINGS.discover_careers or session is None:
            return "", "", Platform.UNKNOWN

        website = str(record.get("website") or "").strip()
        if not website:
            return "", "", Platform.UNKNOWN

        # Imported here: career_finder imports the HTTP stack, which the engine
        # itself has no need of.
        from crawler.career_finder import find_careers_url

        try:
            found = find_careers_url(website, session)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - discovery is best effort
            logger.opt(exception=True).debug("Discovery failed for {}", website)
            return "", "", Platform.UNKNOWN

        if not found or found == exclude:
            return "", "", Platform.UNKNOWN

        platform = detect_platform(found)
        if platform is Platform.UNKNOWN:
            return "", "", Platform.UNKNOWN

        return found, "discovered", platform

    @staticmethod
    def _maybe_record_diagnostics(result: CrawlResult) -> None:
        """Write an evidence dump when an unrecognised board could not be read.

        Only unrecognised platforms are recorded: a Workday board returning
        nothing is a Workday question, whereas an unreadable generic page is
        exactly the case a future adapter would fix.

        Args:
            result: The company's outcome.
        """
        if not SETTINGS.diagnostics or result.jobs:
            return
        if result.platform not in {Platform.GENERIC_HTML, Platform.UNKNOWN}:
            return
        if not result.seed_url:
            return

        # Imported here so a run with diagnostics off never pulls in the
        # browser machinery the module reaches for.
        from crawler.diagnostics import record_unknown

        try:
            record_unknown(
                company=result.company,
                url=result.seed_url,
                platform=result.platform.value,
                error=result.error or "crawled successfully; board advertises no jobs",
            )
        except Exception:  # noqa: BLE001 - diagnostics must never end a run
            logger.opt(exception=True).debug("Could not record diagnostics for {}", result.company)

    def crawl_all(
        self,
        records: Iterable[Mapping[str, str]],
        max_workers: Optional[int] = None,
    ) -> List[CrawlResult]:
        """Crawl every company, concurrently by default.

        The crawl is almost entirely network wait, so running companies in
        parallel is the single biggest thing that shortens a run. Each worker
        gets its own HTTP session, because :class:`requests.Session` is not
        documented thread-safe, and releases it — along with any browser it
        launched — before it exits. A per-host delay keeps the pool from
        converging on one vendor.

        Args:
            records: Company records from
                :func:`crawler.csv_reader.read_companies`.
            max_workers: Companies to crawl at once. Defaults to the run's
                :data:`~config.settings.SETTINGS` value. ``1`` restores the
                strictly sequential behaviour of version 1, including sharing
                one session across the whole run.

        Returns:
            One :class:`CrawlResult` per record, in input order — failures
            included, so nothing about a run is silently dropped.
        """
        pending = list(records)
        workers = max(1, int(max_workers if max_workers is not None else SETTINGS.max_workers))

        if workers == 1 or len(pending) <= 1:
            results = self._crawl_sequentially(pending)
        else:
            results = self._crawl_concurrently(pending, min(workers, len(pending)))

        self._log_summary(results)
        return results

    def _crawl_sequentially(self, records: Sequence[Mapping[str, str]]) -> List[CrawlResult]:
        """Crawl every company on this thread, sharing one session.

        Args:
            records: Company records.

        Returns:
            One result per record, in input order.
        """
        session = self._session_factory() if self._session_factory is not None else None
        results: List[CrawlResult] = []

        try:
            for record in records:
                results.append(self.crawl_company(record, session=session))
        finally:
            self._release(session)

        return results

    def _crawl_concurrently(
        self, records: Sequence[Mapping[str, str]], workers: int
    ) -> List[CrawlResult]:
        """Crawl companies across a fixed pool of worker threads.

        A hand-rolled pool rather than :class:`concurrent.futures.ThreadPoolExecutor`
        for one reason: a worker must tear down its own browser, and Playwright's
        synchronous API can only be closed from the thread that opened it. An
        executor offers no per-thread finaliser; a plain worker loop does.

        Args:
            records: Company records.
            workers: Number of threads. Already clamped by the caller.

        Returns:
            One result per record, in input order.
        """
        results: List[Optional[CrawlResult]] = [None] * len(records)
        work: "queue.Queue[Tuple[int, Mapping[str, str]]]" = queue.Queue()
        for item in enumerate(records):
            work.put(item)

        def run() -> None:
            """Drain the queue, then release everything this thread opened."""
            session = self._session_factory() if self._session_factory is not None else None
            try:
                while True:
                    try:
                        index, record = work.get_nowait()
                    except queue.Empty:
                        return
                    # Each slot is written by exactly one worker, so the list
                    # needs no lock of its own.
                    results[index] = self.crawl_company(record, session=session)
            finally:
                self._release(session)
                self._release_browser()

        logger.info("Crawling {} company(ies) across {} worker(s)", len(records), workers)

        threads = [
            threading.Thread(target=run, name=f"crawler-{index}", daemon=True)
            for index in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # A worker can only leave a slot empty by dying outside crawl_company,
        # which contains everything an adapter can raise. Recording it beats
        # returning a shorter list than the caller's input.
        return [
            result
            if result is not None
            else CrawlResult(
                company=str(records[index].get("company") or ""),
                platform=Platform.UNKNOWN,
                error="worker thread did not report a result",
            )
            for index, result in enumerate(results)
        ]

    @staticmethod
    def _release(session: Optional[object]) -> None:
        """Close a session if it has a ``close``.

        Args:
            session: The session, or ``None`` when no factory was injected.
        """
        close = getattr(session, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _release_browser() -> None:
        """Close the browser this thread launched, if it launched one."""
        if not SETTINGS.browser_fallback:
            return

        try:
            from utils.browser import close_current_thread

            close_current_thread()
        except Exception:  # noqa: BLE001 - teardown must never end a run
            logger.debug("Could not release the browser on this thread", exc_info=True)

    def crawl(self, records: Iterable[Mapping[str, str]]) -> List[Job]:
        """Crawl every company and return the postings, deduplicated.

        Args:
            records: Company records from
                :func:`crawler.csv_reader.read_companies`.

        Returns:
            Every job found across every company, in crawl order, with repeats
            removed. Companies that failed contribute nothing but do not stop
            the run — inspect :meth:`crawl_all` when the per-company outcome
            matters.
        """
        jobs: List[Job] = []
        seen: set = set()

        for result in self.crawl_all(records):
            for job in result.jobs:
                if job.key in seen:
                    continue
                seen.add(job.key)
                jobs.append(job)

        return jobs

    @staticmethod
    def _log_summary(results: Sequence[CrawlResult]) -> None:
        """Log a one-shot account of how a run went.

        Args:
            results: Every result the run produced.
        """
        total_jobs = sum(len(result.jobs) for result in results)
        failed = [result for result in results if not result.ok]

        by_platform: Dict[str, int] = {}
        for result in results:
            if result.jobs:
                by_platform[result.platform.value] = (
                    by_platform.get(result.platform.value, 0) + len(result.jobs)
                )

        logger.success(
            "Crawl finished: {} job(s) from {} of {} company(ies)",
            total_jobs,
            len(results) - len(failed),
            len(results),
        )

        if by_platform:
            breakdown = ", ".join(
                f"{platform}={count}"
                for platform, count in sorted(by_platform.items(), key=lambda item: -item[1])
            )
            logger.info("Jobs by platform: {}", breakdown)

        if failed:
            reasons: Dict[str, int] = {}
            for result in failed:
                reasons[result.error or "unknown"] = reasons.get(result.error or "unknown", 0) + 1
            logger.warning(
                "{} company(ies) produced nothing; top reasons: {}",
                len(failed),
                ", ".join(
                    f"{reason} (x{count})"
                    for reason, count in sorted(reasons.items(), key=lambda item: -item[1])[:5]
                ),
            )
