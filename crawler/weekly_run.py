"""Crawl the master company list and write everything the weekly sheet needs.

    python -m crawler.weekly_run --limit 25 --workers 4 --dry-run
    python -m crawler.weekly_run --limit 25 --workers 4
    python -m crawler.weekly_run --resume

This is the command Windows Task Scheduler will call. It does the whole weekly
job: read the companies, crawl them through the existing version 2 engine, work
out what changed, and write the current jobs, the history, the week's changes,
the run record, the failures and the dashboard.

**The version 2 engine is used unchanged.** Companies are handed to it in
batches, exactly the records :func:`crawler.csv_reader.read_companies` used to
produce, and its worker pool, retry policy, per-host throttle, browser fallback
and failure isolation all behave as they always have. Nothing in
:mod:`crawler.crawler_engine` or in any adapter needed to change for version 3.

**Google Sheets is read in bulk, once.** The whole point of batching here rather
than crawling company-by-company against the sheet is that a run of 7,570
companies costs roughly a dozen Sheets calls in total, not 7,570. The API allows
sixty reads a minute; an N+1 pattern would spend the entire quota inside the
first minute and then stall for the remaining five hours. Concretely: the
company list is read once before the crawl, the job ledger once after it, and
every write is batched.

**Progress is checkpointed locally.** :mod:`crawler.checkpoint` records which
companies are done in ``state/checkpoint.json`` after every batch, so an
interrupted run resumes rather than restarting. That file is the one piece of
state kept outside the spreadsheet, because a resume that depends on the network
fails in exactly the circumstances it exists for.

**The sheet is written once, at the end.** A five-hour crawl accumulates its
observations in memory and applies them in one pass. That keeps the comparison
consistent — a job cannot be counted new by one batch and closed by another —
and keeps the write inside the quota.

**There is a second execution mode, behind ``--queue``.** Everything above
describes the JSON flow, which remains the default. With ``--queue`` the work
comes from the SQLite queue in :mod:`store.queue` instead of from a sliced
Python list::

    python -m crawler.weekly_run --queue --workers 20 --dry-run

What changes is where work comes from and where outcomes go — the roster is
synchronised into SQLite and enqueued, workers *claim* companies atomically
under a lease, and each company's fate is recorded through
:class:`~store.queue.CrawlQueue` with a verdict from
:class:`crawler.retry.RetryPolicy`. What does not change is the crawl itself:
the same engine, the same adapters, the same batch loop body, the same
resolution and filter passes, and the same single batched write to Sheets at
the end. The JSON checkpoint is still written in queue mode, deliberately, so
the two can be compared during the migration rather than swapped blind.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from loguru import logger

from config.settings import SETTINGS, configure, default_workers
from crawler.checkpoint import (
    DEFAULT_CHECKPOINT_PATH,
    STATUS_DONE,
    STATUS_FAILED,
    Checkpoint,
)
from crawler.crawler_engine import CrawlerEngine, CrawlResult, Outcome
from crawler.keywords import KeywordSet, load_keywords
from crawler.job_filters import (
    NARROWING_TYPES,
    DetectionMethod,
    FilterSet,
    detect_filters,
    detect_filters_rendered,
    technology_options,
)
from crawler.observations import observations_from_result
from crawler.resolve import Resolution, resolve_company
from crawler.retry import RetryPolicy, Verdict, classify
from sheets.capacity import CapacityGuard, measure
from store import Database, migrate
from store.database import DEFAULT_DATABASE_PATH
from store.queue import DEFAULT_LEASE_SECONDS, CrawlQueue, QueueItem, QueueState

# Aliased on purpose. `sheets.companies.CompanyRepository` and
# `sheets.jobs.JobRepository` are imported under their own names inside
# __init__, and two things called CompanyRepository in one module is how a
# sheet write ends up in SQLite or the reverse.
from store.repositories import CompanyRepository as StoreCompanyRepository
from store.repositories import JobRepository as StoreJobRepository
from utils.blocking import Block, classify_text
from utils.clock import iso, run_id_for, utc_now, week_of
from utils.http import build_session, get_text

__all__ = ["EmptyRosterError", "RunSummary", "WeeklyRun", "main"]

#: Project root, so paths resolve from any working directory.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

#: Companies handed to the engine at a time. The batch is the unit of
#: checkpointing, so this is the worst case for work repeated after an
#: interruption — a couple of minutes, against a five-hour run.
DEFAULT_BATCH_SIZE: int = 200

#: HTTP attempts per request. Matches the version 2 bulk default: across
#: thousands of companies a board needing four attempts is down rather than
#: busy, and the retries cost more than they recover.
DEFAULT_RETRIES: int = 2

#: Minimum seconds between two crawls of one host, as version 2 uses.
DEFAULT_PER_HOST_DELAY: float = 0.35

#: Characters kept in one cell. Google's ceiling is 50,000 and a board with a
#: thousand offices would sail past it; truncating here is what stops one
#: company's location list rejecting the whole batched write.
_MAX_CELL: int = 2000

#: Attempts one company gets per run before it is failed regardless of what the
#: retry policy says. The policy already caps itself; this is the belt to its
#: braces, because a policy that never gives up would otherwise mean a run that
#: never ends.
DEFAULT_MAX_ATTEMPTS: int = 3

#: An HTTP status quoted inside a recorded error, for the attempt log.
_STATUS_IN_ERROR: re.Pattern = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)


class EmptyRosterError(RuntimeError):
    """There was nothing to crawl, and that is not a successful crawl.

    Raised rather than returned because every caller would otherwise have to
    remember to check, and the one that forgot would report a green run that
    read nothing. The three ways to get here — an empty sheet, a sheet of rows
    that name no URL at all, and a queue in which every company already
    finished — are all operator-fixable, so the message names the fix.
    """


def _status_in(error: str) -> int:
    """The HTTP status an error message quotes, if it quotes one.

    Args:
        error: The recorded failure.

    Returns:
        The status, or ``0``. Recorded on the attempt so a run can be triaged
        by status without re-reading the log.
    """
    found = _STATUS_IN_ERROR.search(str(error or ""))
    return int(found.group(1)) if found else 0


def _default_owner() -> str:
    """A name for this worker, so a stuck claim can be attributed.

    Returns:
        Host and process id, which is enough to find the machine that died.
    """
    try:
        host = socket.gethostname() or "unknown"
    except OSError:  # pragma: no cover - a host with no name
        host = "unknown"
    return f"{host}:{os.getpid()}"


@dataclass
class RunSummary:
    """Everything the run learned, and what it wrote.

    Attributes:
        run_id: The run's identifier.
        week_start: Monday of the ISO week it belongs to.
        week_end: The Sunday after.
        started_at: When it began.
        seconds: How long the crawl itself took.
        companies_total: How many companies the master list holds.
        companies_attempted: How many this run crawled.
        companies_succeeded: How many were read successfully.
        companies_failed: How many could not be read.
        companies_with_jobs: How many advertised anything.
        companies_no_jobs: How many were read and were empty.
        observations: Postings observed, after deduplication.
        tech_observations: ...of which are technology roles.
        changes: The weekly comparison, once applied.
        failures: One record per company that could not be read.
        blockers: Blocker label to company count.
        companies_unusable: How many rows named no website and no URL.
        boards_discovered: How many boards discovery worked out from a website.
        boards_from_sheet: How many were already named in the sheet.
        boards_unresolved: How many could not be resolved at all.
        filters_checked: How many boards were read for their search controls.
        filters_with_controls: ...of which offered at least one.
        filters_narrowing: ...of which offered one that could separate
            technology roles from the rest. The others offer a keyword box or a
            location list, which cannot, so this is the number that says whether
            crawling filtered URLs would be worth doing at all.
        filters_found: Controls found in total.
        filters_tech_options: Options among them that name technology work.
        filters_blocked: Boards that could not be read for filters at all.
        filters_rendered: Boards whose controls needed the browser to appear.
        filter_seconds: Wall-clock time filter detection added to the run.
        resumed: Whether this run continued an earlier attempt.
        interrupted: Whether it stopped early on a signal.
        queue_mode: Whether work came from SQLite rather than from a list.
        queue_enqueued: Companies added to the queue by this run.
        queue_requeued: Finished companies returned to ``pending`` by
            ``--requeue``, so a new week re-crawls them.
        queue_released: Stale claims a dead run left behind, recovered here.
        queue_claimed: Companies this run took off the queue.
        queue_retry_wait: Failures parked for a later attempt.
        queue_blocked: Companies a site actively refused.
        jobs_persisted: Postings written to the local ``jobs`` table.
        jobs_closed_locally: Postings closed there, only for companies this run
            actually read.
    """

    run_id: str
    week_start: str = ""
    week_end: str = ""
    started_at: str = ""
    seconds: float = 0.0
    companies_total: int = 0
    companies_attempted: int = 0
    companies_succeeded: int = 0
    companies_failed: int = 0
    companies_with_jobs: int = 0
    companies_no_jobs: int = 0
    observations: int = 0
    tech_observations: int = 0
    companies_unusable: int = 0
    boards_discovered: int = 0
    boards_from_sheet: int = 0
    boards_unresolved: int = 0
    filters_checked: int = 0
    filters_with_controls: int = 0
    filters_narrowing: int = 0
    filters_found: int = 0
    filters_tech_options: int = 0
    filters_blocked: int = 0
    filters_rendered: int = 0
    filter_seconds: float = 0.0
    changes: Optional[Any] = None
    failures: List[Dict[str, str]] = field(default_factory=list)
    blockers: Dict[str, int] = field(default_factory=dict)
    resumed: bool = False
    interrupted: bool = False
    queue_mode: bool = False
    queue_enqueued: int = 0
    queue_requeued: int = 0
    queue_released: int = 0
    queue_claimed: int = 0
    queue_retry_wait: int = 0
    queue_blocked: int = 0
    jobs_persisted: int = 0
    jobs_closed_locally: int = 0

    # -- incremental persistence -------------------------------------------
    # A run that writes once can read its totals off the single comparison it
    # made. A run that writes per batch has no single comparison, so the totals
    # are carried here instead and the batch's observations are then released.
    segments: int = 0
    jobs_new: int = 0
    jobs_reopened: int = 0
    jobs_closed: int = 0
    jobs_still_active: int = 0
    sheet_rows_written: int = 0
    capacity_blocked: Dict[str, str] = field(default_factory=dict)

    # -- the operator's keyword list ---------------------------------------
    # Recorded so a verdict can be traced back to the list that produced it.
    # Once the list is edited weekly, "why was this posting classified that
    # way" is a question about which version of the list was in effect.
    keywords_active: int = 0
    keyword_categories: int = 0
    keyword_fingerprint: str = ""

    # -- reconciliation of the current-state tabs --------------------------
    current_jobs_removed: int = 0
    failures_resolved: int = 0

    @property
    def success_rate(self) -> Optional[float]:
        """Share of attempted companies that were read.

        Returns:
            A percentage, or ``None`` when nothing was attempted. ``None``
            rather than ``0.0`` because a run that crawled nothing did not fail
            at everything, and reporting a 100% failure rate for an empty
            company list is simply wrong.
        """
        if not self.companies_attempted:
            return None
        return self.companies_succeeded / self.companies_attempted * 100.0

    @property
    def rate_text(self) -> Tuple[str, str]:
        """The success and failure rates, rendered for display.

        Returns:
            ``(success, failure)``, both ``"n/a"`` when nothing was attempted.
        """
        rate = self.success_rate
        if rate is None:
            return "n/a", "n/a"
        return f"{rate:.1f}%", f"{100.0 - rate:.1f}%"

    def counts(self) -> Dict[str, int]:
        """The counting columns ``WEEKLY_RUNS`` holds.

        Returns:
            Field name to value.
        """
        summary = self.changes.summary() if self.changes is not None else {}

        # A segmented run accumulated its totals batch by batch; a single-write
        # run reads them off the one comparison it made.
        if self.segments:
            jobs_active = self.observations
            jobs_new = self.jobs_new
            jobs_closed = self.jobs_closed
        else:
            jobs_active = summary.get("jobs_observed", self.observations)
            jobs_new = summary.get("jobs_new", 0)
            jobs_closed = summary.get("jobs_closed", 0)

        return {
            "companies_total": self.companies_total,
            "companies_checked": self.companies_attempted,
            "companies_succeeded": self.companies_succeeded,
            "companies_failed": self.companies_failed,
            "companies_with_jobs": self.companies_with_jobs,
            "companies_no_jobs": self.companies_no_jobs,
            "jobs_active": jobs_active,
            "jobs_new": jobs_new,
            "jobs_closed": jobs_closed,
            "companies_discovered": 0,
        }

    def dashboard_sections(self) -> List[Tuple[str, List[Tuple[str, object]]]]:
        """The metrics the dashboard shows, grouped for display.

        Returns:
            ``(section, [(metric, value), ...])`` in display order.
        """
        summary = self.changes.summary() if self.changes is not None else {}
        duration = _elapsed(self.seconds)

        sections: List[Tuple[str, List[Tuple[str, object]]]] = [
            (
                "Companies",
                [
                    ("Total companies", self.companies_total),
                    ("Companies checked", self.companies_attempted),
                    ("Succeeded", self.companies_succeeded),
                    ("Failed", self.companies_failed),
                    ("With jobs", self.companies_with_jobs),
                    ("No open jobs", self.companies_no_jobs),
                    ("Unusable rows", self.companies_unusable),
                ],
            ),
            (
                "Discovery of boards",
                [
                    ("Board already in the sheet", self.boards_from_sheet),
                    ("Board discovered from the website", self.boards_discovered),
                    ("Could not be resolved", self.boards_unresolved),
                ],
            ),
            (
                "Jobs",
                [
                    ("Active jobs", summary.get("jobs_observed", self.observations)),
                    ("Technology jobs", self.tech_observations),
                    ("New this week", summary.get("jobs_new", 0)),
                    ("Reopened this week", summary.get("jobs_reopened", 0)),
                    ("Closed this week", summary.get("jobs_closed", 0)),
                    ("Unchanged", summary.get("jobs_still_active", 0)),
                    ("Re-linked", summary.get("jobs_relinked", 0)),
                    ("Removed from CURRENT_JOBS", self.current_jobs_removed),
                ],
            ),
            (
                "Discovery",
                [
                    ("Companies discovered", 0),
                ],
            ),
            (
                "Run",
                [
                    ("Run ID", self.run_id),
                    ("Week", f"{self.week_start} to {self.week_end}"),
                    ("Duration", duration),
                    ("Success rate", self.rate_text[0]),
                    ("Failure rate", self.rate_text[1]),
                    ("Closures withheld", summary.get("closures_withheld", 0)),
                    ("Companies not read", summary.get("companies_not_read", 0)),
                    ("IT keywords in effect", self.keywords_active),
                    ("Keyword categories", self.keyword_categories),
                    ("Keyword list fingerprint", self.keyword_fingerprint or "none"),
                    ("Failures resolved", self.failures_resolved),
                    ("Resumed", "yes" if self.resumed else "no"),
                    ("Interrupted", "yes" if self.interrupted else "no"),
                ],
            ),
        ]

        # Only when the run actually looked. A section of zeroes would read
        # as "this board has no filters" rather than "nobody asked", and the
        # dashboard is written from exactly these rows.
        if self.filters_checked or self.filters_blocked:
            sections.insert(
                3,
                (
                    "Board filters",
                    [
                        ("Boards read for filters", self.filters_checked),
                        ("Boards offering filters", self.filters_with_controls),
                        ("...that narrow by department", self.filters_narrowing),
                        ("Filters found", self.filters_found),
                        ("Technology options", self.filters_tech_options),
                        ("Needed the browser", self.filters_rendered),
                        ("Could not be read", self.filters_blocked),
                        ("Time spent on filters", _elapsed(self.filter_seconds)),
                    ],
                ),
            )

        # Only in queue mode. In the JSON flow every one of these is zero, and
        # a section of zeroes reads as "the queue did nothing" rather than
        # "there was no queue".
        if self.queue_mode:
            sections.append(
                (
                    "SQLite queue",
                    [
                        ("Enqueued", self.queue_enqueued),
                        ("Returned to pending", self.queue_requeued),
                        ("Stale claims recovered", self.queue_released),
                        ("Claimed this run", self.queue_claimed),
                        ("Waiting to retry", self.queue_retry_wait),
                        ("Refused by the site", self.queue_blocked),
                        ("Postings stored locally", self.jobs_persisted),
                        ("Postings closed locally", self.jobs_closed_locally),
                    ],
                )
            )

        if self.blockers:
            sections.append(
                (
                    "Failures",
                    [(label, count) for label, count in sorted(
                        self.blockers.items(), key=lambda item: -item[1]
                    )],
                )
            )

        return sections


def _is_usable(record: Mapping[str, str]) -> bool:
    """Whether a master-list row gives the crawler somewhere to start.

    Company Name plus Website is the minimum viable row: the careers page, the
    applicant tracking system and the board URL are all discovered from there.
    A name on its own is not enough, and pretending otherwise would report a
    company as crawled when nothing was attempted.

    Args:
        record: A company record from the master list.

    Returns:
        ``True`` when the row names a website, a careers page or a board.
    """
    return any(
        str(record.get(field) or "").strip()
        for field in ("website", "career_url", "it_link")
    )


def _most_common(values: Sequence[str]) -> str:
    """The commonest non-blank value, for summarising a company's postings.

    Args:
        values: Values observed across the company's jobs.

    Returns:
        The commonest, or ``""`` when there were none. Ties break on the first
        seen, which keeps the result stable between runs.
    """
    counts: Dict[str, int] = {}
    for value in values:
        cleaned = str(value or "").strip()
        if cleaned:
            counts[cleaned] = counts.get(cleaned, 0) + 1

    if not counts:
        return ""
    return max(counts, key=lambda value: counts[value])


def _elapsed(seconds: float) -> str:
    """Render a duration as ``1h 04m 09s``.

    Args:
        seconds: Elapsed seconds.

    Returns:
        The formatted duration.
    """
    hours, rest = divmod(int(seconds), 3600)
    minutes, remainder = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {remainder:02d}s"
    if minutes:
        return f"{minutes}m {remainder:02d}s"
    return f"{remainder}s"


class _Heartbeat:
    """Keeps a batch's claims alive while the engine is still crawling them.

    The lease exists so a worker that dies does not strand its companies. That
    same lease will happily expire underneath a worker that is merely *slow* —
    a browser rescue on a large board is minutes, not seconds — and another run
    would then claim a company this one is actively crawling. So a claim is
    refreshed for as long as it is being worked on.

    The thread opens its own connection, which is what :class:`store.Database`
    hands out per thread. It stops on the first error rather than retrying: a
    failing heartbeat means the lease will lapse, which the queue already
    handles, and a thread spinning on a dead database helps nobody.

    Args:
        queue: The queue holding the claims.
        company_keys: What this batch is working on.
        interval: Seconds between refreshes.
    """

    def __init__(self, queue: CrawlQueue, company_keys: Sequence[str], interval: float) -> None:
        self._queue = queue
        self._keys = list(company_keys)
        self._interval = max(0.001, float(interval))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name="queue-heartbeat", daemon=True)
        self.beats = 0

    def start(self) -> "_Heartbeat":
        """Begin refreshing.

        Returns:
            Self, so a caller can start and hold it in one expression.
        """
        if self._keys:
            self._thread.start()
        return self

    def _beat(self) -> None:
        """Refresh every claim until asked to stop."""
        try:
            while not self._stop.wait(self._interval):
                try:
                    for key in self._keys:
                        self._queue.heartbeat(key)
                except Exception as exc:  # noqa: BLE001 - a lapsed lease is recoverable
                    logger.debug("Heartbeat stopped: {}", exc)
                    return
                self.beats += 1
        finally:
            # :class:`store.Database` hands out a connection per thread and
            # closes only the caller's, so this thread has to close its own.
            # Left open it keeps a Windows lock on the database file for the
            # life of the process, and the next run finds it busy.
            try:
                self._queue.database.close()
            except Exception as exc:  # noqa: BLE001 - closing is best effort
                logger.debug("Could not close the heartbeat connection: {}", exc)

    @property
    def running(self) -> bool:
        """Whether the thread is still alive."""
        return self._thread.is_alive()

    def stop(self) -> None:
        """Stop refreshing and wait for the thread to notice."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)


class WeeklyRun:
    """Runs one week's crawl and writes its results to the spreadsheet.

    Args:
        client: The Sheets client. Injected, so the whole run can be exercised
            against an in-memory spreadsheet in the tests.
        engine: The crawl engine. Injected for the same reason; defaults to a
            real one configured from :data:`~config.settings.SETTINGS`.
        checkpoint_path: Where progress is recorded.
        batch_size: Companies per batch, and so per checkpoint.
        tech_only: Whether ``CURRENT_JOBS`` holds technology roles only.
        session_factory: Builds the HTTP session board resolution uses.
            Injected so a test can supply a fake and never reach the network.
        filter_detector: Reads one board's search controls, as
            ``(company_key, board_url) -> FilterSet``. Injected for the same
            reason; defaults to fetching the board and handing the markup to
            :func:`crawler.job_filters.detect_filters`.
        database: The local store. Required by, and only used by, queue mode.
        queue_mode: Take work from :class:`~store.queue.CrawlQueue` rather than
            from a sliced list. Off by default: the JSON flow stays the shipped
            behaviour until the queue has been run in anger.
        retry_policy: Decides the fate of a failed company. Injected so a test
            can make a retry immediate; defaults to
            :class:`crawler.retry.RetryPolicy`.
        lease_seconds: How long a claim is honoured before another run may take
            it. ``0`` recovers every outstanding claim at startup, which is
            what a deliberate "the last run died" recovery wants.
        heartbeat_seconds: How often a claim in flight is refreshed. ``0``
            derives a third of the lease, which leaves room for two missed
            beats before anything is reclaimed.
        owner: This worker's name, recorded on its claims. Defaults to host
            and process id.
        retry_poll_seconds: How long to wait for parked retries to come due
            before giving up on them for this run. ``0`` — the default — never
            waits: a company in ``retry_wait`` is durable and the next
            invocation picks it up, which is the whole point of a queue that
            outlives the process.
    """

    def __init__(
        self,
        client: Any,
        engine: Optional[CrawlerEngine] = None,
        checkpoint_path: Path | str = DEFAULT_CHECKPOINT_PATH,
        batch_size: int = DEFAULT_BATCH_SIZE,
        tech_only: bool = True,
        session_factory: Optional[Any] = None,
        filter_detector: Optional[Any] = None,
        database: Optional[Database] = None,
        queue_mode: bool = False,
        retry_policy: Optional[RetryPolicy] = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = 0.0,
        owner: str = "",
        retry_poll_seconds: float = 0.0,
    ) -> None:
        from sheets.companies import CompanyRepository
        from sheets.jobs import JobRepository
        from sheets.runs import DashboardRepository, FailureRepository, RunRepository

        if queue_mode and database is None:
            raise ValueError(
                "queue_mode needs a Database; construct one with "
                "store.Database(path) and pass it as database="
            )

        self.client = client
        self.engine = engine
        self.checkpoint_path = Path(checkpoint_path)
        self.batch_size = max(1, int(batch_size))
        self.tech_only = tech_only
        self._session_factory = session_factory or (
            lambda: build_session(retries=SETTINGS.retries)
        )

        self.companies = CompanyRepository(client)
        self.jobs = JobRepository(client)
        self.runs = RunRepository(client)
        self.failures = FailureRepository(client)
        self.dashboard = DashboardRepository(client)

        self._detector = filter_detector or self._read_filters

        self._stopping = False

        # The browser budget is spent by whichever worker gets there first, so
        # it is claimed under a lock rather than counted per thread.
        self._render_lock = threading.Lock()
        self._renders_left = 0

        # -- the queue, when there is one ------------------------------------
        self.database = database
        self.queue_mode = bool(queue_mode)
        self.owner = owner or _default_owner()
        self.lease_seconds = max(0.0, float(lease_seconds))
        self.heartbeat_seconds = (
            float(heartbeat_seconds) if heartbeat_seconds > 0
            else max(1.0, self.lease_seconds / 3.0)
        )
        self.retry_poll_seconds = max(0.0, float(retry_poll_seconds))
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=DEFAULT_MAX_ATTEMPTS)

        self.queue: Optional[CrawlQueue] = None
        self._store_companies: Optional[StoreCompanyRepository] = None
        self._store_jobs: Optional[StoreJobRepository] = None

        # The repositories come up whenever there is a database, and the queue
        # only when the queue is asked for. They used to be built together, so
        # the durable job ledger was reachable only through queue mode -- which
        # meant the default path crawled twelve thousand companies and stored
        # none of them until the very end. Storage and work distribution are
        # separate decisions; only the second one is opt-in.
        if database is not None:
            self._store_companies = StoreCompanyRepository(database)
            self._store_jobs = StoreJobRepository(database)
        if self.queue_mode and database is not None:
            self.queue = CrawlQueue(database)

        #: Companies **this run** read successfully. The set handed to the
        #: weekly comparison, and the reason a resumed run does not close the
        #: first segment's postings. The JSON flow's equivalent is
        #: ``Checkpoint.crawled_this_session``, and the two mean the same thing.
        self.crawled_this_run: Set[str] = set()

        #: The batch currently claimed, by company key, so an outcome can find
        #: the attempt number the queue handed out with the claim.
        self._claimed: Dict[str, QueueItem] = {}
        self._heartbeat: Optional[_Heartbeat] = None
        self.heartbeats = 0

    # -- shutdown ------------------------------------------------------------

    def request_stop(self) -> None:
        """Ask the run to stop after the batch in flight.

        Called from a signal handler, so it does no work beyond setting a flag:
        the batch finishes, its results are checkpointed, and the run exits
        having lost nothing.
        """
        if not self._stopping:
            logger.warning("Stop requested — finishing the current batch, then exiting")
        self._stopping = True

    # -- the run -------------------------------------------------------------

    def execute(
        self,
        limit: int = 0,
        resume: bool = True,
        dry_run: bool = False,
        run_id: Optional[str] = None,
        extra_keywords: Iterable[str] = (),
        use_queue: bool = False,
        requeue: bool = False,
    ) -> RunSummary:
        """Crawl the master list and write the results.

        Args:
            limit: Crawl only the first N companies. ``0`` means all of them.
            resume: Continue an unfinished run from this week, if there is one.
            dry_run: Crawl and compare, but write nothing to the spreadsheet.
            run_id: Override the generated run identifier.
            extra_keywords: Additional phrases counting as technology roles.
            use_queue: Take work from the durable SQLite queue rather than from
                the roster and the JSON checkpoint. Off by default: the JSON
                path is what two live runs have verified, and it stays the
                default until this one has earned the same. Redundant when the
                runner was built with ``queue_mode=True``; either turns the
                queue on.
            requeue: Return companies that finished in an earlier run to
                ``pending`` before claiming, so a new week crawls them again.
                Off by default, because the common case is resuming an
                interrupted run. ``resume=False`` implies it.

        Returns:
            What the run did.

        Raises:
            EmptyRosterError: In queue mode, when there is nothing left to
                claim. A run that crawls nothing is never reported as a
                successful crawl of nothing.
        """
        started = utc_now()
        week_start, week_end = week_of(started)

        # --- one bulk read of the company list ------------------------------
        # This also gives an identity to any row typed straight into the sheet:
        # every other tab joins on Company Key, and a hand-added company has
        # none until something derives one. Both jobs share the single read.
        every, duplicate_rows = self.companies.prepare_roster(dry_run=dry_run)
        if duplicate_rows:
            logger.info("{} duplicate company row(s) will be crawled once", duplicate_rows)
        total = len(every)

        # A row needs somewhere to start. Company Name plus Website is enough --
        # everything else is discovered -- but a name on its own is not, and
        # silently counting such rows as crawled would overstate the run.
        roster = [record for record in every if _is_usable(record)]
        unusable = [record for record in every if not _is_usable(record)]

        if unusable:
            logger.warning(
                "{} row(s) name no website, careers page or board and cannot be crawled",
                len(unusable),
            )

        # --- the operator's keyword list, read once -------------------------
        # Once per run, never per company. This is the whole reason the tab is
        # worth having: a term added to IT_KEYWORDS on Thursday changes what
        # Saturday recognises, with no code change and no deployment. Reading
        # it per posting would instead spend the entire sixty-calls-a-minute
        # quota on configuration.
        keywords = self._load_keywords()
        extra = tuple(extra_keywords)
        classifier_terms: Tuple[Any, ...] = (*keywords.all(), *extra)

        # Either switch turns the queue on: the constructor's `queue_mode` and
        # this call's `use_queue` are the same decision reached from two
        # directions, and a runner given a database but neither flag stays on
        # the JSON path.
        queue_on = bool((self.queue_mode or use_queue) and self.database is not None)

        # The limit applies to usable companies, so --limit 5 crawls five
        # companies rather than five rows of which some are unusable.
        #
        # In queue mode it caps what is *claimed* instead, and the whole roster
        # is still enqueued. A limited run is then a smaller bite of the same
        # queue rather than a smaller queue -- which is what makes `--limit 3`
        # safe to try against the real database.
        if limit > 0 and not queue_on:
            roster = roster[:limit]

        checkpoint = Checkpoint.resume_or_start(
            run_id or run_id_for(started),
            total=len(roster),
            path=self.checkpoint_path,
            resume=resume,
        )

        summary = RunSummary(
            run_id=checkpoint.run_id,
            week_start=week_start,
            week_end=week_end,
            started_at=iso(started),
            companies_total=total,
            companies_unusable=len(unusable),
            resumed=checkpoint.completed > 0,
            keywords_active=len(keywords),
            keyword_categories=len(keywords.categories()),
            keyword_fingerprint=keywords.fingerprint() if keywords else "",
        )

        # Where the next batch comes from. The queue replaces this one thing
        # and nothing else -- in particular `checkpoint` still tracks what this
        # segment read, because `crawled_this_session` is what stops a blocked
        # company's jobs being closed, and that decision belongs in one place.
        queue = (
            self._prepare_queue(roster, resume=resume, requeue=requeue, summary=summary)
            if queue_on
            else None
        )
        summary.queue_mode = queue is not None

        pending = [
            record
            for record in roster
            if record.get("company_key") not in checkpoint.companies
        ]

        logger.info(
            "Run {}: {} company(ies) on the list, {} to crawl{}",
            checkpoint.run_id,
            total,
            len(pending),
            f" (resuming, {checkpoint.completed} already done)" if summary.resumed else "",
        )

        # --- the durable store, for the JSON path too ------------------------
        # Postings reference their company, so the roster has to exist before
        # the first one is stored. Queue mode already does this inside
        # _prepare_queue; this is the same upsert for the path that does not
        # build a queue. Idempotent, and a blank cell never clears a stored
        # value, so running it every week costs nothing.
        incremental = self._store_jobs is not None and not dry_run
        if incremental and queue is None and self._store_companies is not None:
            stored_rows = self._store_companies.upsert_many(
                {
                    "company_key": record.get("company_key"),
                    "company_name": record.get("company"),
                    "website": record.get("website"),
                    "career_url": record.get("career_url"),
                    "it_link": record.get("it_link"),
                }
                for record in roster
            )
            logger.info("Durable store: {} company row(s) synchronised", stored_rows)

        # Cleared until something has actually been written. The flag means
        # "every company this file records has had its postings persisted", and
        # it is set at the two points where that becomes true: after a batch is
        # persisted, and after a graceful stop writes everything it accumulated.
        # A checkpoint saved by a run that did neither is not resumable.
        checkpoint.durable = False

        # --- the cell budget -------------------------------------------------
        capacity = None
        if not dry_run:
            try:
                report = measure(self.client)
                capacity = CapacityGuard(report)
                logger.info("Spreadsheet capacity: {}", report.describe())
            except Exception as exc:  # pragma: no cover - a metadata read failing
                # A budget that cannot be measured must not stop a crawl whose
                # results are durable anyway. The writes proceed unguarded and
                # Google's own error is what surfaces if the ceiling is hit.
                logger.warning("Could not measure the spreadsheet's capacity: {}", exc)

        if not dry_run:
            self.runs.start(
                companies_total=total,
                mode="weekly",
                run_id=checkpoint.run_id,
                dry_run=False,
            )

        # --- crawl, batch by batch ------------------------------------------
        # Armed here rather than in __init__ so a runner reused for a second
        # execute() gets a fresh budget rather than an exhausted one.
        self._renders_left = max(0, int(SETTINGS.filter_render_budget))

        engine = self.engine or CrawlerEngine(
            session_factory=lambda: build_session(retries=SETTINGS.retries)
        )

        observations: List[Dict[str, str]] = []
        seen_keys: Set[str] = set()
        failures: List[Dict[str, str]] = []
        company_outcomes: Dict[str, Dict[str, object]] = {}
        clock = time.monotonic()

        # The queue replaces this one expression and nothing else. Everything
        # below it is the batch loop as the JSON path has always run it.
        sources = (
            self._queue_batches(queue, limit, summary)
            if queue is not None
            else self._batches(pending)
        )

        for batch in sources:
            if self._stopping:
                summary.interrupted = True
                break

            # Only in queue mode: a claim is a lease, and a browser rescue on a
            # large board outlives the default one. Refreshed for as long as
            # the batch is actually being worked on.
            self._start_heartbeat(queue)

            # What the ledger knew about these companies *before* this batch
            # touched them. Read here rather than after the crawl because
            # _absorb stores each company's postings as it goes, and comparing
            # against a ledger this batch has already updated would report
            # every new posting as one it always knew about.
            batch_keys = [str(record.get("company_key") or "") for record in batch]
            prior_known = self._known_before(batch_keys, checkpoint.run_id)

            try:
                # Work out where each company's jobs live before crawling it. A
                # row that already names a board resolves without a request; one
                # that names only a website has its careers page and applicant
                # tracking system discovered here, and both are written back to
                # the sheet so next week's run needs no discovery at all.
                resolutions = self._resolve_batch(batch, summary)
                prepared = [
                    resolutions[str(record.get("company_key") or "")].to_record(record)
                    for record in batch
                ]

                results = engine.crawl_all(prepared)

                # Read after the crawl rather than before it, because the URL
                # the engine actually used is the board -- resolution only
                # predicted one, and the engine may have fallen through to a
                # better seed. Off by default, in which case this returns {}
                # without a request.
                filter_sets = self._detect_filters_batch(batch, results, summary)

                for record, result in zip(batch, results):
                    self._absorb(
                        record,
                        result,
                        checkpoint.run_id,
                        observations,
                        seen_keys,
                        failures,
                        company_outcomes,
                        checkpoint,
                        summary,
                        classifier_terms,
                        resolutions.get(str(record.get("company_key") or "")),
                        filter_sets.get(str(record.get("company_key") or "")),
                    )
            except BaseException:
                # Ctrl-C, or the engine giving up. Whatever is still claimed was
                # never read, and saying so beats leaving it to a lease.
                self._abandon_claims(queue)
                raise
            finally:
                self._stop_heartbeat()

            # --- persist, then checkpoint ------------------------------------
            # The order is the whole guarantee. Postings reached SQLite inside
            # _absorb; this writes the operator-facing tabs; only then is the
            # batch recorded as done. A crash anywhere before the checkpoint
            # replays the batch, and every write it replays is keyed -- by
            # job_key, by company_key, by (run, job, change) -- so the replay
            # converges instead of duplicating.
            #
            # Checkpointing first would be the bug this run was stopped for:
            # companies marked done whose postings exist nowhere.
            if incremental:
                self._persist_segment(
                    observations=observations,
                    failures=failures,
                    company_outcomes=company_outcomes,
                    known=prior_known,
                    batch_keys=batch_keys,
                    checkpoint=checkpoint,
                    summary=summary,
                    capacity=capacity,
                )
                # True only now: this batch's postings are in SQLite and its
                # rows are in the sheet, so recording its companies as done
                # cannot strand them.
                checkpoint.durable = True

            # Deliberately not on a dry run. Writing the checkpoint would make
            # the next real run skip companies this one only pretended to crawl.
            if not dry_run:
                self._save(checkpoint)

            logger.info(
                "Progress: {}/{} company(ies), {} posting(s) so far",
                checkpoint.completed,
                len(roster),
                summary.observations if incremental else len(observations),
            )

            # --- release the batch -------------------------------------------
            # Written and checkpointed, so the records themselves are no longer
            # needed. `seen_keys` deliberately stays: it is the cross-batch
            # dedup set, it holds keys rather than postings, and dropping it
            # would let one posting be counted twice across two batches.
            if incremental:
                observations.clear()
                failures.clear()
                company_outcomes.clear()

        # In queue mode the loop ends by running out of claimable work, so a
        # stop request is noticed here rather than at the top of an iteration
        # that never happens. Guarded, so the JSON path keeps deciding this
        # exactly where it always has.
        if queue is not None and self._stopping:
            summary.interrupted = True

        # Rows that could not be crawled at all still belong in FAILURES: the
        # fix is a cell in the sheet, and an operator cannot fix what is not
        # reported.
        from sheets.runs import failure_record

        for record in unusable:
            failures.append(
                failure_record(
                    company_key=str(record.get("company_key") or ""),
                    company_name=str(record.get("company") or ""),
                    website=str(record.get("website") or ""),
                    error=(
                        "AdapterUrlError: MASTER_COMPANIES row has no Website, "
                        "Career Page URL or IT Link, so there is nothing to crawl"
                    ),
                    run_id=checkpoint.run_id,
                )
            )

        summary.seconds = time.monotonic() - clock

        if incremental:
            # Every batch has already been written. What is left in the lists is
            # the tail the loop never reached -- the unusable rows appended just
            # above -- so the totals come from the running counters and only the
            # remainder is written here.
            #
            # *Appended*, not assigned. `_persist_segment` accumulates each
            # batch's failures onto the summary and then clears the list it was
            # given, so at this point `failures` holds only the unusable rows.
            # Assigning it discarded every real failure the run recorded, and
            # the dashboard's failure breakdown then reported the handful of
            # rows naming no URL as though they were the whole story.
            summary.failures = [*summary.failures, *failures]
            summary.blockers = self._blocker_counts(summary.failures)
            if capacity is not None:
                summary.capacity_blocked = dict(capacity.blocked)
            if failures:
                self.failures.upsert(failures, dry_run=False)
        else:
            summary.observations = len(observations)
            summary.tech_observations = sum(
                1 for item in observations if item.get("is_tech")
            )
            summary.failures = failures
            summary.blockers = self._blocker_counts(failures)

            # --- write everything, once -------------------------------------
            applied = self.jobs.apply(
                observations,
                # This segment's companies, not every company the checkpoint has
                # ever recorded. On a resume the earlier segment's companies are
                # not re-crawled, so their postings are absent from
                # `observations` -- and handing their keys to the comparison
                # would read that absence as "these jobs have gone" and close
                # every one of them.
                crawled=checkpoint.crawled_this_session,
                run_id=checkpoint.run_id,
                tech_only=self.tech_only,
                dry_run=dry_run,
            )
            summary.changes = applied.changes

            if not dry_run:
                self.companies.record_crawl(company_outcomes, dry_run=False)
                self.failures.replace(failures, dry_run=False)

        if not dry_run:
            # Before the dashboard, so it reports what reconciliation did.
            # Only on the incremental path: the whole-run path still writes
            # CURRENT_JOBS with replace() and FAILURES with replace(), both of
            # which already leave the tab holding exactly the current state.
            if incremental:
                self._reconcile_reports(checkpoint, summary)

            self.dashboard.write(
                summary.dashboard_sections(), week_start=week_start, dry_run=False
            )
            notes = f"{summary.observations} posting(s) observed"
            notes += (
                f"; {summary.keywords_active} IT keyword(s) in effect"
                f" [{summary.keyword_fingerprint}]"
                if summary.keywords_active
                else "; no IT keywords in effect"
            )
            if summary.capacity_blocked:
                # Recorded rather than raised. The postings are in SQLite; what
                # ran out was a tab, and the operator needs to know which.
                notes += (
                    " — sheet capacity reached: "
                    + "; ".join(
                        f"{tab} ({reason})"
                        for tab, reason in sorted(summary.capacity_blocked.items())
                    )
                )

            self.runs.finish(
                checkpoint.run_id,
                status="interrupted" if summary.interrupted else "done",
                counts=summary.counts(),
                notes=notes,
                dry_run=False,
            )

            if summary.interrupted:
                # Kept in place, so the next invocation resumes rather than
                # restarting the companies this one already covered. Safe to
                # resume because everything above has just been written: a
                # stop that runs to here has persisted what it accumulated,
                # which a crash in the middle of the loop has not.
                checkpoint.durable = True
                self._save(checkpoint)
            else:
                checkpoint.archive()

        logger.success(
            "Run {} finished: {} company(ies), {} posting(s), {}",
            checkpoint.run_id,
            summary.companies_attempted,
            summary.observations,
            (
                f"{summary.jobs_new} new, {summary.jobs_reopened} reopened, "
                f"{summary.jobs_closed} closed, {summary.jobs_still_active} unchanged"
                if incremental
                else applied.describe()
            ),
        )
        return summary

    # -- the durable queue ---------------------------------------------------

    def _prepare_queue(
        self,
        roster: Sequence[Mapping[str, str]],
        resume: bool,
        requeue: bool,
        summary: RunSummary,
    ) -> CrawlQueue:
        """Bring the store up, copy the roster into it and fill the queue.

        The sheet stays the source of *which companies exist*; the queue is the
        source of *what is left to do*. So the roster just read is upserted --
        and because a blank cell never clears a stored value, a board that
        discovery found locally survives a sheet which has not been told about
        it yet -- and then anything not already queued is queued.

        Only usable rows are copied. A row naming no website, careers page or
        board has nowhere to start, and a queue in which every company is
        uncrawlable is worse than a smaller one.

        Args:
            roster: The usable companies the sheet holds.
            resume: Whether this continues an interrupted run. A resume leaves
                finished companies alone; ``resume=False`` returns them to
                ``pending``, because a new week has to crawl them again.
            requeue: Ask for that requeue explicitly, without also saying this
                is a fresh run.
            summary: Counters, updated in place.

        Returns:
            The queue to take work from.

        Raises:
            EmptyRosterError: When nothing can be claimed.
        """
        database = self.database
        if database is None:  # pragma: no cover - the caller has checked
            raise ValueError("queue mode needs a Database")

        # Idempotent, and a no-op on a database already at the current version.
        # Done here rather than asked of the operator, so `--queue` works
        # against a file that has never been opened before.
        migrate(database)

        # Built here when the constructor did not, which is the shape a caller
        # passing `database=` without `queue_mode=True` produces.
        if self.queue is None:
            self.queue = CrawlQueue(database)
        if self._store_companies is None:
            self._store_companies = StoreCompanyRepository(database)
        if self._store_jobs is None:
            self._store_jobs = StoreJobRepository(database)
        queue = self.queue

        if not roster:
            raise EmptyRosterError(
                "No company in MASTER_COMPANIES names a website, careers page "
                "or board, so there is nothing to crawl. Add a Website or "
                "Career Page URL to at least one row."
            )

        # The store spells it company_name; the sheet spells it company.
        self._store_companies.upsert_many(
            {**record, "company_name": record.get("company") or ""}
            for record in roster
        )

        # Before anything is claimed. A run that died left its companies
        # `running`, and no one else may take them until the lease lapses --
        # so a run that starts by recovering them is the difference between a
        # crash costing one batch and it costing every company that was in
        # flight.
        summary.queue_released = queue.release_stale(lease_seconds=self.lease_seconds)

        # Idempotent, which is what makes a resume safe: a company already in
        # the queue keeps whatever state it has.
        summary.queue_enqueued = queue.enqueue_all(run_id=summary.run_id)

        # Which also means a second week against last week's database adds
        # nothing at all, because every company is already there in a terminal
        # state. Something has to say "this is a new run, try them again", and
        # this is it. Never automatic: a resume must not re-crawl what the
        # interrupted segment already finished.
        if requeue or not resume:
            summary.queue_requeued = queue.reset_finished()

        counts = queue.stats()
        claimable = (
            counts.get(QueueState.PENDING.value, 0)
            + counts.get(QueueState.RETRY_WAIT.value, 0)
        )

        # Refused only when every company has reached a settled verdict.
        # `running` belongs to another worker and `skipped` to a run that was
        # interrupted before it could crawl them -- neither is a finished
        # queue, and refusing to start over them would turn one interrupted run
        # into a stuck one.
        unsettled = claimable + (
            counts.get(QueueState.RUNNING.value, 0)
            + counts.get(QueueState.SKIPPED.value, 0)
        )
        if not unsettled:
            # The failure this guard exists for: every company is already
            # `succeeded` from last week, so `enqueue_all` correctly adds
            # nothing -- and without this the run would crawl nothing and
            # report success.
            raise EmptyRosterError(
                f"Every company in the queue has finished "
                f"({counts.get(QueueState.SUCCEEDED.value, 0)} succeeded, "
                f"{counts.get(QueueState.FAILED.value, 0)} failed, "
                f"{counts.get(QueueState.BLOCKED.value, 0)} blocked) and there "
                f"is nothing left to claim. Pass --requeue to crawl them again."
            )

        logger.info(
            "Queue ready: {} enqueued, {} returned to pending, {} stale claim(s) "
            "recovered, {} claimable",
            summary.queue_enqueued,
            summary.queue_requeued,
            summary.queue_released,
            claimable,
        )
        return queue

    @property
    def heartbeat_running(self) -> bool:
        """Whether a heartbeat thread is currently refreshing claims.

        Returns:
            ``True`` only between the start and the end of a batch.
        """
        return self._heartbeat is not None and self._heartbeat.running

    def _queue_batches(
        self,
        queue: CrawlQueue,
        limit: int,
        summary: RunSummary,
    ) -> Iterator[List[Mapping[str, str]]]:
        """Claim companies from the queue, a batch at a time, until none are left.

        This is the one thing the queue replaces: where the next batch comes
        from. The batch it yields is the same list of company records the JSON
        path yields, so the loop body around it does not know the difference.

        Claiming happens just before the batch is crawled rather than up front,
        which is what lets a stop request leave the rest of the queue
        ``pending`` for the next run instead of stranding it ``running``.

        Args:
            queue: The queue to take work from.
            limit: Cap on how many companies to claim in total. ``0`` means
                drain the queue.
            summary: Counters, updated in place.

        Yields:
            Batches of company records, at most :attr:`batch_size` each.
        """
        taken = 0

        # Checked before claiming, not after: a batch claimed and then
        # abandoned would sit `running` until its lease lapsed.
        while not self._stopping:
            wanted = self.batch_size
            if limit > 0:
                wanted = min(wanted, limit - taken)
                if wanted <= 0:
                    return

            items = queue.claim(self.owner, limit=wanted, lease_seconds=self.lease_seconds)

            if not items:
                # Nothing claimable now. There may still be companies parked in
                # `retry_wait` whose turn has not come; whether to wait for
                # them is the operator's call, and the default is not to.
                if self._wait_for_retries():
                    continue
                return

            taken += len(items)
            summary.queue_claimed += len(items)

            # Held so an outcome can find the attempt number the queue handed
            # out with the claim.
            self._claimed = {item.company_key: item for item in items}
            yield [item.as_record() for item in items]

    def _wait_for_retries(self) -> bool:
        """Wait for a parked retry to come due, if one is due soon enough.

        Returns:
            Whether it is worth claiming again. ``False`` ends the run, which
            is the right answer for a retry parked minutes away: the company is
            durable in SQLite and the next invocation picks it up, which is the
            whole point of a queue that outlives the process.
        """
        if self.retry_poll_seconds <= 0 or self.database is None:
            return False

        row = self.database.one(
            "SELECT MIN(next_attempt_at) AS due FROM crawl_queue WHERE state = 'retry_wait'"
        )
        due = float((row or {}).get("due") or 0.0)
        if not due:
            return False

        wait = due - time.time()
        if wait <= 0:
            return True
        if wait > self.retry_poll_seconds:
            return False

        time.sleep(wait)
        return True

    def _start_heartbeat(self, queue: Optional[CrawlQueue]) -> None:
        """Keep this batch's claims alive while the engine works on them.

        Args:
            queue: The queue holding the claims, or ``None`` on the JSON path,
                in which case there is nothing to refresh.
        """
        if queue is None or not self._claimed:
            return
        self._heartbeat = _Heartbeat(
            queue, list(self._claimed), self.heartbeat_seconds
        ).start()

    def _stop_heartbeat(self) -> None:
        """Stop refreshing, and keep the count for the report."""
        if self._heartbeat is None:
            return
        self._heartbeat.stop()
        self.heartbeats += self._heartbeat.beats
        self._heartbeat = None

    def _abandon_claims(self, queue: Optional[CrawlQueue]) -> None:
        """Give up the batch in flight when the run dies part-way through it.

        Marked skipped rather than released. Released would mean ``pending``,
        and the next run would crawl a company this one had already handed to
        the engine — the interruption would silently double the work rather
        than merely stop it. Skipped says what happened, keeps the company out
        of the failure report it did not earn, and ``--requeue`` brings it back.

        Args:
            queue: The queue holding the claims, if there is one.
        """
        if queue is None:
            return
        for company_key in self._claimed:
            try:
                queue.skip(company_key, reason="interrupted mid-batch")
            except Exception as exc:  # noqa: BLE001 - the lease recovers it anyway
                logger.debug("Could not release {}: {}", company_key, exc)
        self._claimed = {}

    def _resolve_batch(
        self,
        batch: Sequence[Mapping[str, str]],
        summary: RunSummary,
    ) -> Dict[str, Resolution]:
        """Work out where each company in a batch publishes its jobs.

        Resolution is pure HTTP — no browser, no adapter — so it runs across a
        small thread pool rather than serially. A company whose row already
        names a board resolves without a request at all, which is why a sheet
        that has been through one run costs nothing here on the next.

        Args:
            batch: The companies about to be crawled.
            summary: Counters, updated in place.

        Returns:
            Company key to resolution.
        """
        from concurrent.futures import ThreadPoolExecutor

        resolutions: Dict[str, Resolution] = {}

        # Companies that already name a board need no network call, so they are
        # settled first and never reach the pool.
        pending: List[Mapping[str, str]] = []
        for record in batch:
            key = str(record.get("company_key") or "")
            settled = resolve_company(record, session=None, discover=False)

            if settled.source == "sheet" and settled.it_link:
                resolutions[key] = settled
                summary.boards_from_sheet += 1
            else:
                pending.append(record)

        if not pending:
            return resolutions

        # Discovery is opt-in for the whole process, exactly as version 2's own
        # careers-page discovery is: config.settings ships inert so that
        # importing this module cannot cause it to start probing websites, and
        # the offline test suite relies on that. main() turns it on for a run.
        if not SETTINGS.discover_careers:
            for record in pending:
                key = str(record.get("company_key") or "")
                resolutions[key] = resolve_company(record, session=None, discover=False)
                if resolutions[key].resolved:
                    summary.boards_from_sheet += 1
                else:
                    summary.boards_unresolved += 1
            return resolutions

        workers = max(1, min(SETTINGS.max_workers, len(pending)))

        def resolve_one(record: Mapping[str, str]) -> Tuple[str, Resolution]:
            """Resolve one company on its own session."""
            key = str(record.get("company_key") or "")
            session = self._session_factory()
            try:
                return key, resolve_company(record, session=session, discover=True)
            except Exception:  # noqa: BLE001 - one company must not end the run
                logger.opt(exception=True).debug("Resolution failed for {}", key)
                return key, Resolution(company_key=key, detail="resolution raised")
            finally:
                close = getattr(session, "close", None)
                if callable(close):
                    close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, resolution in pool.map(resolve_one, pending):
                resolutions[key] = resolution

                if resolution.discovered:
                    summary.boards_discovered += 1
                elif resolution.resolved:
                    summary.boards_from_sheet += 1
                else:
                    summary.boards_unresolved += 1

        return resolutions


    # -- board filters -------------------------------------------------------

    def _claim_render(self) -> bool:
        """Take one visit from the run's browser budget.

        Returns:
            ``True`` when a visit was claimed, ``False`` when the budget is
            spent. Claiming rather than checking is what keeps a pool of
            workers from all deciding at once that there is one visit left.
        """
        with self._render_lock:
            if self._renders_left <= 0:
                return False
            self._renders_left -= 1
            return True

    def _read_filters(self, company_key: str, board_url: str) -> FilterSet:
        """Read one board's search controls.

        Fetching the board again is the cost of this: version 2's engine does
        not keep the markup it read, and threading it back out through every
        adapter to save a request would change the one part of version 3 that
        is deliberately unchanged. So a company that was crawled successfully
        costs one further GET, and a company that was not costs nothing —
        which is why the caller only offers boards that were read.

        Args:
            company_key: The company, for the log.
            board_url: The URL the crawl actually used.

        Returns:
            What was found. Never raises: a board that cannot be fetched comes
            back empty with :attr:`FilterSet.blocked` set, so "no controls" is
            never confused with "never seen".
        """
        found = FilterSet(source_url=board_url)
        session = self._session_factory()

        try:
            markup = get_text(session, board_url)
        except Exception as exc:  # noqa: BLE001 - one board must not end the run
            block = classify_text(str(exc))
            found.blocked = (block.value if block is not Block.NONE else str(exc))[:120]
            logger.debug("Filters for {} could not be read: {}", company_key, found.blocked)
            markup = ""
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

        if markup:
            found = detect_filters(markup, board_url)

        # ADP, UltiPro and Eightfold serve markup with no controls in it at all
        # and build every one client-side, so on those boards the static read
        # finding nothing is the expected outcome rather than the answer. The
        # browser settles it, and the budget stops that costing the run hours.
        #
        # The test is for a *narrowing* control rather than for any control at
        # all, and that distinction is worth the extra visits: a company's own
        # careers page almost always carries a site-wide search box, which is
        # read as a keyword filter and would otherwise be enough to call the
        # board done and skip the browser -- on an ADP board whose department
        # list only exists after its JavaScript has run.
        if not found.by_type(*NARROWING_TYPES) and self._claim_render():
            rendered = detect_filters_rendered(board_url)

            if rendered.by_type(*NARROWING_TYPES):
                return rendered

            # Nothing better was found. A rendered page is still the stronger
            # evidence when the static one yielded nothing at all, because it
            # saw the board as a visitor does; a blocker it reports is kept for
            # the same reason.
            if rendered.blocked:
                found.blocked = found.blocked or rendered.blocked
            elif not found.filters:
                found = rendered

        return found

    def _detect_filters_batch(
        self,
        batch: Sequence[Mapping[str, str]],
        results: Sequence[CrawlResult],
        summary: RunSummary,
    ) -> Dict[str, FilterSet]:
        """Read the search controls of every board this batch could read.

        Nothing here influences what is crawled or what is written to
        ``CURRENT_JOBS``: the filters are recorded against the company and that
        is all. Using the filtered URLs to crawl fewer pages is the obvious
        next step and is deliberately not this step, because it would change
        which postings a run finds.

        Args:
            batch: The companies just crawled.
            results: What the engine produced for them, in the same order.
            summary: Counters, updated in place.

        Returns:
            Company key to what was found. Empty when detection is off.
        """
        if not SETTINGS.detect_filters:
            return {}

        from concurrent.futures import ThreadPoolExecutor

        # Only boards that were actually read. A company whose crawl failed
        # would fail this fetch too, and spending a request to prove it twice
        # is the kind of thing that turns a five-hour run into a six-hour one.
        targets: List[Tuple[str, str]] = []
        for record, result in zip(batch, results):
            key = str(record.get("company_key") or "")
            readable = result.ok or result.outcome is Outcome.NO_JOBS
            if key and result.seed_url and readable:
                targets.append((key, result.seed_url))

        if not targets:
            return {}

        clock = time.monotonic()
        workers = max(1, min(SETTINGS.max_workers, len(targets)))

        def read_one(target: Tuple[str, str]) -> Tuple[str, FilterSet]:
            """Read one board, treating any failure as an empty result."""
            key, url = target
            try:
                return key, self._detector(key, url)
            except Exception:  # noqa: BLE001 - one company must not end the run
                logger.opt(exception=True).debug("Filter detection raised for {}", key)
                return key, FilterSet(source_url=url, blocked="detection raised")

        found: Dict[str, FilterSet] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, filters in pool.map(read_one, targets):
                found[key] = filters

        summary.filter_seconds += time.monotonic() - clock
        summary.filters_checked += len(found)

        for filters in found.values():
            if filters.filters:
                summary.filters_with_controls += 1
                summary.filters_found += filters.count
                if filters.by_type(*NARROWING_TYPES):
                    summary.filters_narrowing += 1
                summary.filters_tech_options += len(technology_options(filters))
                if DetectionMethod.RENDERED_DOM in filters.methods:
                    summary.filters_rendered += 1
            elif filters.blocked:
                summary.filters_blocked += 1

        return found

    def _batches(self, records: Sequence[Mapping[str, str]]) -> Iterable[List[Mapping[str, str]]]:
        """Split the pending companies into batches.

        Args:
            records: Companies still to crawl.

        Yields:
            Lists of at most :attr:`batch_size` companies.
        """
        for start in range(0, len(records), self.batch_size):
            yield list(records[start : start + self.batch_size])

    def _absorb(
        self,
        record: Mapping[str, str],
        result: CrawlResult,
        run_id: str,
        observations: List[Dict[str, str]],
        seen_keys: Set[str],
        failures: List[Dict[str, str]],
        company_outcomes: Dict[str, Dict[str, object]],
        checkpoint: Checkpoint,
        summary: RunSummary,
        extra_keywords: Iterable[str],
        resolution: Optional[Resolution] = None,
        filters: Optional[FilterSet] = None,
    ) -> None:
        """Fold one company's result into the run's accumulated state.

        Args:
            record: The company as the master list holds it.
            result: What the engine produced for it.
            run_id: The run.
            observations: Accumulated observations, extended in place.
            seen_keys: Job keys already produced, extended in place.
            failures: Accumulated failures, extended in place.
            company_outcomes: Per-company crawl notes, extended in place.
            checkpoint: Progress, recorded in place.
            summary: Counters, updated in place.
            extra_keywords: Additional phrases counting as technology roles.
            resolution: What was worked out about where this company's jobs
                live, so the discovered URLs can be written back to the sheet.
            filters: The board's search controls, when they were read. ``None``
                when detection is off, which leaves every filter cell in the
                sheet exactly as it was.
        """
        from sheets.runs import failure_record

        company_key = str(record.get("company_key") or "")
        summary.companies_attempted += 1

        outcome = result.outcome
        readable = result.ok or outcome is Outcome.NO_JOBS

        if readable:
            summary.companies_succeeded += 1
            checkpoint.record(company_key, STATUS_DONE)
        else:
            summary.companies_failed += 1
            checkpoint.record(company_key, STATUS_FAILED)

        if result.jobs:
            summary.companies_with_jobs += 1
        elif readable:
            summary.companies_no_jobs += 1

        # Kept separately as well as accumulated, because the durable store
        # records one company at a time and closure is scoped to one company.
        mine = observations_from_result(
            result,
            company=record,
            run_id=run_id,
            extra_keywords=extra_keywords,
            seen=seen_keys,
        )
        observations.extend(mine)

        # What to write back to MASTER_COMPANIES. Resolution supplies the URLs
        # and the platform; the postings themselves supply the location detail,
        # which is the only place a crawl can honestly learn it.
        updates: Dict[str, object] = {"last_outcome": outcome.value}

        if resolution is not None:
            updates.update(resolution.sheet_updates())

        # The URL actually crawled is authoritative over what resolution
        # predicted: the engine may have fallen through to a second seed, or
        # discovered a better board than resolution found.
        if result.seed_url:
            from crawler.resolve import is_ats

            if is_ats(result.platform):
                # A vendor's host is the board, whatever resolution guessed.
                updates["it_link"] = result.seed_url
                updates["platform"] = result.platform.value
                # ...and the careers page stays whatever resolution found, so a
                # company's own /careers URL is not replaced by its ADP link.
                updates.setdefault("career_url", result.seed_url)
            else:
                updates.setdefault("career_url", result.seed_url)

        jobs = list(getattr(result, "jobs", None) or [])
        updates["active_jobs"] = str(len(jobs))

        if jobs:
            # Only ever filled from what the boards published. Industry is not
            # among these: nothing in a job posting states it, and inventing one
            # is worse than leaving the cell for the operator.
            for field_name, attribute in (
                ("country", "country"),
                ("location", "location"),
                ("department", "department"),
            ):
                common = _most_common([str(getattr(job, attribute, "") or "") for job in jobs])
                if common:
                    updates[field_name] = common

        # The board's own search controls, when this run looked. Written as
        # separate cells rather than one blob so the operator can sort and
        # filter on them, which is the whole reason they are in the sheet.
        if filters is not None:
            reported = filters.summary()
            updates.update(
                {
                    "filters_detected": "TRUE" if reported["filters_detected"] else "FALSE",
                    "filter_count": str(reported["filter_count"]),
                    "filter_types": str(reported["filter_types"]),
                    "filter_labels": str(reported["filter_labels"]),
                    "filter_values": str(reported["filter_values"])[:_MAX_CELL],
                    "filter_detection_method": str(reported["filter_detection_method"]),
                    "filter_confidence": str(reported["filter_confidence"]),
                    "filter_blocked": str(reported["blocked"]),
                }
            )

        company_outcomes[company_key] = updates

        if not readable:
            failures.append(
                failure_record(
                    company_key=company_key,
                    company_name=str(record.get("company") or ""),
                    website=str(record.get("website") or ""),
                    crawled_url=result.seed_url,
                    platform=result.platform.value,
                    error=result.error or "",
                    run_id=run_id,
                )
            )

        # -- the queue, when this company came from one -----------------------
        # Guarded on the claim rather than on the mode, so a company the JSON
        # path is crawling can never reach the queue by accident.
        if self.queue is not None and company_key in self._claimed:
            self._record_in_queue(company_key, result, readable, len(mine), run_id, summary)

        # -- and the durable store, whenever there is one ---------------------
        # This is where a posting becomes durable, and it happens here rather
        # than at the end of the run: the batch's checkpoint is written after
        # this, so a company is never recorded as done before its jobs are
        # stored. Independent of the queue, because the JSON path needs the
        # guarantee just as much.
        if readable:
            # The set handed to the weekly comparison. Successes only: a
            # company that was not read proves nothing about what its board
            # still advertises, and offering it would close every posting
            # the crawl failed to see.
            self.crawled_this_run.add(company_key)
            self._store_postings(company_key, mine, run_id, summary)

    def _record_in_queue(
        self,
        company_key: str,
        result: CrawlResult,
        readable: bool,
        jobs: int,
        run_id: str,
        summary: RunSummary,
    ) -> None:
        """Record one company's fate in the queue.

        A read board is a success even when it advertises nothing: an empty
        board is a fact about the company, not a failure of the crawl. Anything
        else is classified, judged by the retry policy, and parked, blocked or
        failed accordingly.

        Args:
            company_key: The company.
            result: What the engine produced for it.
            readable: Whether the board was read at all.
            jobs: How many postings it yielded.
            run_id: The run.
            summary: Counters, updated in place.
        """
        queue = self.queue
        item = self._claimed.get(company_key)
        if queue is None or item is None:  # pragma: no cover - the caller checks
            return

        if readable:
            queue.succeed(company_key, jobs=jobs, run_id=run_id)
            return

        error = result.error or ""
        verdict = self.retry_policy.decide(classify(error), item.attempts)

        # The belt to the policy's braces. A policy that never gives up would
        # otherwise mean a company claimed, failed and reclaimed for as long as
        # the run lasts -- which is to say, for ever.
        if verdict.retry and item.attempts >= self.retry_policy.max_attempts:
            logger.debug(
                "{}: {} attempt(s) is the cap, failing rather than retrying",
                company_key,
                item.attempts,
            )
            verdict = Verdict(
                retry=False, delay=0.0, blocked=verdict.blocked, reason=verdict.reason
            )

        queue.fail(
            company_key,
            reason=verdict.reason,
            retry_in=verdict.delay,
            retryable=verdict.retry,
            http_status=_status_in(error),
            run_id=run_id,
            blocked=verdict.blocked,
        )

        if verdict.retry:
            summary.queue_retry_wait += 1
        elif verdict.blocked:
            summary.queue_blocked += 1

    def _store_postings(
        self,
        company_key: str,
        postings: Sequence[Mapping[str, object]],
        run_id: str,
        summary: RunSummary,
    ) -> None:
        """Write one company's postings to the local store and close the rest.

        The observations already carry the identity
        :mod:`crawler.observations` derived from the board the crawl *actually
        used*, and :meth:`store.repositories.JobRepository.record_many` honours
        a supplied ``job_key``. That is what stops one posting acquiring two
        primary keys when discovery finds a better board than the sheet holds.

        Called only for a company that was read, which is what makes the
        closure below safe: it is the same rule the weekly comparison applies,
        for the same reason.

        Args:
            company_key: The company.
            postings: Its observations from this run.
            run_id: The run.
            summary: Counters, updated in place.
        """
        repository = self._store_jobs
        if repository is None:  # pragma: no cover - only when there is no database
            return

        if postings:
            counts = repository.record_many(postings, run_id=run_id)
            summary.jobs_persisted += counts.get("inserted", 0) + counts.get("updated", 0)

        # Scoped to this company, and only to a run that read it. A posting
        # this crawl did not see on a board this crawl did read has gone.
        summary.jobs_closed_locally += repository.close_missing(
            company_key,
            [str(posting.get("job_key") or "") for posting in postings],
            run_id=run_id,
        )

    def _known_before(self, company_keys: Sequence[str], run_id: str) -> List[Dict[str, object]]:
        """What the ledger held for these companies before this batch ran.

        Rows this same run inserted are excluded. That matters on a replay: a
        batch that stored its postings and then died before its checkpoint is
        crawled again, and if its own insertions counted as prior knowledge the
        replay would classify every one of them as an unremarkable posting it
        had always known about, and the weekly log would never record them.
        Dropping them restores the state the first attempt started from, which
        is what makes the replay produce the same answer.

        Args:
            company_keys: The companies about to be crawled.
            run_id: The run in progress.

        Returns:
            Ledger rows, in the field names ``sheets.jobs`` expects. Empty when
            there is no durable store.
        """
        repository = self._store_jobs
        if repository is None:
            return []

        return [
            dict(row)
            for row in repository.for_companies(company_keys)
            if str(row.get("first_run_id") or "") != run_id
        ]

    def _persist_segment(
        self,
        observations: Sequence[Mapping[str, object]],
        failures: Sequence[Mapping[str, str]],
        company_outcomes: Mapping[str, Mapping[str, object]],
        known: Sequence[Mapping[str, object]],
        batch_keys: Sequence[str],
        checkpoint: Checkpoint,
        summary: RunSummary,
        capacity: Optional[Any] = None,
    ) -> None:
        """Write one batch's results to the operator-facing tabs.

        Called after the batch's postings are in SQLite and before its
        checkpoint is written, which is the ordering that makes an interrupted
        run safe to replay.

        Every write here is keyed, so replaying a batch converges on the same
        rows rather than adding a second copy: ``CURRENT_JOBS`` on ``job_key``,
        ``FAILURES`` on ``company_key``, ``MASTER_COMPANIES`` on ``company_key``
        and ``NEW_LAST_WEEK`` on the run, the job and the kind of change.

        ``JOB_HISTORY`` is deliberately not written. SQLite is the ledger now,
        and mirroring several hundred thousand rows into a seventeen-column tab
        is what exhausts the spreadsheet.

        Args:
            observations: The batch's postings.
            failures: The batch's failures.
            company_outcomes: The batch's per-company notes for the roster.
            known: What the ledger held before the batch, from
                :meth:`_known_before`.
            batch_keys: Company keys in this batch.
            checkpoint: Progress, read for the companies actually read.
            summary: Counters, updated in place.
            capacity: The cell budget, consulted before each write.
        """
        summary.segments += 1

        # Only this batch's successes. `crawled_this_session` grows across the
        # whole run, and offering an earlier batch's companies here would let
        # this comparison close postings it never looked at.
        crawled = {key for key in batch_keys if key in checkpoint.crawled_this_session}

        applied = self.jobs.apply(
            observations,
            crawled=crawled,
            run_id=checkpoint.run_id,
            tech_only=self.tech_only,
            dry_run=False,
            known=known,
            incremental=True,
            capacity=capacity,
        )

        # The most recent segment's comparison. Kept because a single-batch run
        # -- which is most of them -- reads exactly as it always did, and
        # because the alternative is accumulating every posting's comparison
        # object for the whole run, which is the memory this change exists to
        # release. Run-level totals live in the counters below, and
        # RunSummary.counts() prefers them once a run has segments.
        summary.changes = applied.changes

        changes = applied.changes.summary() if applied.changes is not None else {}
        summary.jobs_new += int(changes.get("jobs_new", 0))
        summary.jobs_reopened += int(changes.get("jobs_reopened", 0))
        summary.jobs_closed += int(changes.get("jobs_closed", 0))
        summary.jobs_still_active += int(changes.get("jobs_still_active", 0))
        summary.observations += len(observations)
        summary.tech_observations += sum(1 for item in observations if item.get("is_tech"))

        if company_outcomes:
            self.companies.record_crawl(company_outcomes, dry_run=False)

        # Upsert, never replace: a replace would make this batch's failures the
        # only ones the tab holds.
        if failures:
            self.failures.upsert(failures, dry_run=False)
            summary.blockers = self._blocker_counts(
                [*summary.failures, *failures]
            )
            summary.failures = [*summary.failures, *failures]

        if capacity is not None and capacity.blocked:
            summary.capacity_blocked = dict(capacity.blocked)

        # So WEEKLY_RUNS is worth reading during a crawl that takes hours
        # rather than only after it.
        self.runs.update_counts(checkpoint.run_id, summary.counts(), dry_run=False)

    def _load_keywords(self) -> KeywordSet:
        """Read ``IT_KEYWORDS`` once, for the whole run.

        The tab is the configuration; this module only reads it. Called exactly
        once per :meth:`execute`, which is what keeps a twelve-thousand-company
        run to one Sheets call for its keyword list rather than twelve thousand.

        Returns:
            The enabled terms. Empty when the tab is absent, unreadable or
            holds nothing — :func:`crawler.keywords.load_keywords` says so at
            ERROR or WARNING in that case rather than passing an empty list
            along in silence, and the run continues on the built-in tables in
            :mod:`crawler.tech_filter`. Losing the operator's terms is bad;
            losing the crawl because a tab could not be read would be worse.
        """
        try:
            keywords = load_keywords(self.client)
        except Exception as exc:  # noqa: BLE001 - configuration must not end a run
            logger.error(
                "IT_KEYWORDS could not be loaded ({}): {}. Classification falls "
                "back to the built-in tables in crawler.tech_filter alone.",
                type(exc).__name__,
                exc,
            )
            return KeywordSet([])

        if keywords:
            logger.success(
                "IT_KEYWORDS in effect: {} term(s), {} category(ies), fingerprint {}",
                len(keywords),
                len(keywords.categories()),
                keywords.fingerprint(),
            )
        return keywords

    def _reconcile_reports(self, checkpoint: Checkpoint, summary: RunSummary) -> None:
        """Bring the two current-state tabs back in line with the ledger.

        Both tabs promise to show *now*: ``CURRENT_JOBS`` what is open, and
        ``FAILURES`` what is unresolved. An incremental run cannot keep either
        promise by upserting alone — batch two has no reason to revisit a row
        batch one wrote — so a posting that closes and a company that recovers
        both linger. This is the pass that removes them, and it runs once, at
        the end, rather than per batch: a rewrite costs the tail of a tab, and
        paying that sixty-two times to keep a mid-run report tidy is not worth
        the quota.

        Driven by SQLite and by the checkpoint rather than by what this run
        happened to observe, which is what makes it converge:

        * a posting closed **this** run is closed in the ledger, so it goes;
        * a posting closed in an **earlier** run whose removal never happened —
          because that run crashed, or predates this code — is still closed in
          the ledger, so it goes now;
        * a posting the ledger has never heard of is left alone, because
          storage cannot testify that something it never saw has ended;
        * running the same ``run_id`` again finds nothing left to remove and
          writes nothing, so a replay costs one read.

        Neither removal can grow the grid, so neither is asked of the capacity
        guard: :meth:`sheets.storage.TabStore.remove` can only ever leave a tab
        the same size or shorter.

        Failures here are logged and swallowed. A tidying pass that ran after
        sixteen hours of crawling must not be the thing that loses the run.

        Args:
            checkpoint: Progress, for the companies read across every segment
                of this run — not merely this process's, because a company an
                earlier segment read is just as resolved as one this one did.
            summary: Counters, updated in place.
        """
        repository = self._store_jobs

        # -- CURRENT_JOBS: drop what the ledger marks closed ------------------
        if repository is not None:
            try:
                shown = self.jobs.current_job_keys()
                closed = repository.closed_among(shown) if shown else []
                if closed:
                    summary.current_jobs_removed = self.jobs.remove_current(closed)
                    logger.info(
                        "CURRENT_JOBS: removed {} posting(s) the ledger marks closed",
                        summary.current_jobs_removed,
                    )
            except Exception as exc:  # noqa: BLE001 - tidying must not end a run
                logger.warning(
                    "CURRENT_JOBS could not be reconciled ({}): {}. The tab may "
                    "still show postings SQLite has closed; the next run retries.",
                    type(exc).__name__,
                    exc,
                )

        # -- FAILURES: drop the companies that have since been read -----------
        try:
            resolved = checkpoint.crawled_keys
            if resolved:
                summary.failures_resolved = self.failures.resolve(resolved)
                if summary.failures_resolved:
                    logger.info(
                        "FAILURES: removed {} row(s) for companies since read",
                        summary.failures_resolved,
                    )
        except Exception as exc:  # noqa: BLE001 - tidying must not end a run
            logger.warning(
                "FAILURES could not be reconciled ({}): {}. The tab may still "
                "show companies that have since succeeded; the next run retries.",
                type(exc).__name__,
                exc,
            )

        # -- and the run-level breakdown, from the reconciled tab -------------
        # `summary.failures` holds what *this process* recorded, which is the
        # wrong answer for a run that was interrupted and resumed: the earlier
        # segment's failures are real, are still in the tab, and belong in the
        # week's breakdown. The tab has just been brought up to date -- every
        # resolved company removed, every unusable row added -- so it is now
        # exactly "what is unresolved across this whole run", which is what the
        # dashboard's failure section is for.
        try:
            counted = self.failures.counts_by_type()
            if counted or not summary.blockers:
                summary.blockers = counted
        except Exception as exc:  # noqa: BLE001 - fall back to what we counted
            logger.warning(
                "FAILURES could not be counted ({}): {}. The dashboard reports "
                "this process's own failures instead of the whole run's.",
                type(exc).__name__,
                exc,
            )

    def _save(self, checkpoint: Checkpoint) -> None:
        """Write the checkpoint, treating a failure as non-fatal.

        Args:
            checkpoint: Progress so far.
        """
        try:
            checkpoint.save()
        except OSError as exc:
            # Losing a checkpoint costs a repeat, not the results. Stopping the
            # run over it would cost the whole crawl.
            logger.warning("Could not write the checkpoint: {}", exc)

    @staticmethod
    def _blocker_counts(failures: Sequence[Mapping[str, str]]) -> Dict[str, int]:
        """Count failures by their classification.

        Args:
            failures: The failure records.

        Returns:
            Blocker label to company count, largest first.
        """
        counts: Dict[str, int] = {}
        for failure in failures:
            label = str(failure.get("failure_type") or "")
            if label:
                counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: -item[1]))


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
        prog="python -m crawler.weekly_run",
        description="Crawl the master company list and update the weekly spreadsheet.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Crawl only the first N companies")
    parser.add_argument(
        "--workers",
        type=int,
        default=default_workers(),
        help=f"Companies crawled at once (default: {default_workers()})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Companies per checkpoint (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--retries", type=int, default=DEFAULT_RETRIES, help="HTTP attempts per request"
    )
    parser.add_argument(
        "--per-host-delay",
        type=float,
        default=DEFAULT_PER_HOST_DELAY,
        help="Minimum seconds between two crawls of one host",
    )
    parser.add_argument("--no-browser", action="store_true", help="Never fall back to Chromium")
    parser.add_argument(
        "--no-discover", action="store_true", help="Do not search a website for its careers page"
    )
    parser.add_argument(
        "--all-jobs",
        action="store_true",
        help="Put every posting in CURRENT_JOBS, not only technology roles",
    )
    parser.add_argument(
        "--filters",
        action="store_true",
        help=(
            "Also read each board's own search controls into the filter columns "
            "of MASTER_COMPANIES. Costs one extra request per company read"
        ),
    )
    parser.add_argument(
        "--filter-render",
        type=int,
        default=0,
        metavar="N",
        help=(
            "With --filters, allow up to N boards whose controls are built "
            "client-side to be read in the browser (default: 0, never)"
        ),
    )
    parser.add_argument(
        "--queue",
        action="store_true",
        help=(
            "Take work from the durable SQLite queue instead of the JSON "
            "checkpoint. Survives a crash, a reboot and a closed terminal, and "
            "lets several processes share one run. Off by default"
        ),
    )
    parser.add_argument(
        "--database",
        default=None,
        metavar="PATH",
        help="With --queue, where the database lives (default: state/crawler.db)",
    )
    parser.add_argument(
        "--requeue",
        action="store_true",
        help=(
            "With --queue, return companies that finished in an earlier run to "
            "pending, so a new week crawls them again. Off by default, because "
            "the common case is resuming an interrupted run rather than "
            "starting a new one"
        ),
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help=(
            "With --queue, release every outstanding claim before starting, "
            "not just the expired ones. The deliberate \"the last run died\" "
            "switch; do not use it while another run is working"
        ),
    )
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=DEFAULT_LEASE_SECONDS,
        metavar="SECONDS",
        help=(
            "With --queue, how long a claim is honoured before another run may "
            f"take it (default: {DEFAULT_LEASE_SECONDS:.0f})"
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        metavar="N",
        help=(
            "With --queue, attempts one company gets before it is failed "
            f"regardless of the retry policy (default: {DEFAULT_MAX_ATTEMPTS})"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue this week's unfinished run instead of starting over",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore any existing checkpoint and start from the first company",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=DEFAULT_CHECKPOINT_PATH,
        help=f"Checkpoint file (default: {DEFAULT_CHECKPOINT_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Crawl and compare, but write nothing to the spreadsheet",
    )
    parser.add_argument(
        "--spreadsheet", default=None, help="Spreadsheet id or URL (default: from the environment)"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"),
        help="Console log level (default: INFO)",
    )
    parser.add_argument(
        "--log-file", type=Path, default=None, help="Also write a full DEBUG log here"
    )
    return parser.parse_args(list(argv))


def _configure_logging(level: str, log_file: Optional[Path]) -> None:
    """Point loguru at a quiet console and, optionally, a complete file.

    Args:
        level: Console log level.
        log_file: Where the full DEBUG log goes, if anywhere.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<level>{level: <8}</level> | {message}",
        backtrace=False,
        diagnose=False,
    )

    if log_file is None:
        return

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            level="DEBUG",
            rotation="50 MB",
            retention=8,
            encoding="utf-8",
            enqueue=True,
            backtrace=True,
            diagnose=False,
        )
    except OSError as exc:
        print(f"WARNING: could not open the log file {log_file}: {exc}", file=sys.stderr)


def _render(summary: RunSummary, dry_run: bool, api: Optional[Any] = None) -> str:
    """Render the run report for a terminal.

    Args:
        summary: What the run did.
        dry_run: Whether anything was written.
        api: The client's traffic statistics, if available.

    Returns:
        The report as text.
    """
    rule = "=" * 78
    lines = [rule, "WEEKLY RUN — DRY RUN (nothing written)" if dry_run else "WEEKLY RUN", rule]

    for section, metrics in summary.dashboard_sections():
        lines.append("")
        lines.append(f"  {section}")
        lines.append("  " + "-" * 74)
        for metric, value in metrics:
            lines.append(f"    {str(metric)[:38]:<40}{value}")

    if api is not None:
        lines.append("")
        lines.append(f"  Sheets API: {api.describe()}")

    lines.append(rule)
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the weekly crawl.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` on success, ``2`` when the spreadsheet is not configured, ``1``
        on failure, ``130`` when interrupted.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _configure_logging(args.log_level, args.log_file)

    from sheets._cli import connect, report_failure

    connection, code = connect(args)
    if connection is None:
        return code

    # The run's choices, applied once. These are the version 2 defaults; the
    # engine reads them exactly as `python main.py` makes it.
    configure(
        max_workers=max(1, args.workers),
        per_host_delay=max(0.0, args.per_host_delay),
        browser_fallback=not args.no_browser,
        discover_careers=not args.no_discover,
        detect_filters=bool(args.filters),
        filter_render_budget=max(0, args.filter_render) if args.filters else 0,
        diagnostics=False,
        retries=args.retries,
        output_dir=PROJECT_ROOT / "output",
    )

    # Opened for every run, not only --queue. The database is now where a
    # posting becomes durable, and the JSON flow needs that as much as the
    # queue does -- a run holding twelve thousand companies' results in memory
    # until the end is one power cut away from having crawled nothing.
    # `--queue` still decides only where the *work* comes from.
    database: Optional[Database] = None
    if not args.dry_run:
        database = Database(Path(args.database) if args.database else DEFAULT_DATABASE_PATH)
        migrate(database)

    run = WeeklyRun(
        connection.client,
        checkpoint_path=args.checkpoint,
        batch_size=args.batch_size,
        tech_only=not args.all_jobs,
        database=database,
        queue_mode=bool(args.queue),
        # `--recover` means "the last run died": a zero lease makes every
        # outstanding claim stale, so startup recovers all of them.
        lease_seconds=0.0 if args.recover else max(0.0, args.lease_seconds),
        retry_policy=RetryPolicy(max_attempts=max(1, args.max_attempts)),
    )

    # A Ctrl-C or a shutdown finishes the batch in flight and checkpoints it,
    # rather than losing however many hours the run had accumulated.
    def stop(_signum: int, _frame: Any) -> None:
        run.request_stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        received = getattr(signal, name, None)
        if received is not None:
            try:
                signal.signal(received, stop)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                pass

    try:
        summary = run.execute(
            limit=args.limit,
            resume=not args.fresh,
            dry_run=args.dry_run,
            use_queue=bool(args.queue),
            requeue=bool(args.requeue),
        )
    except Exception as exc:  # noqa: BLE001 - reported with the API's wording
        return report_failure(exc, connection.account)
    finally:
        # Released here rather than left to the interpreter, because Windows
        # keeps a lock on an open SQLite file and the next run would find it
        # busy.
        if database is not None:
            database.close()

    print(_render(summary, args.dry_run, connection.client.stats))

    if summary.interrupted:
        print("\nInterrupted. Resume with:  python -m crawler.weekly_run --resume")
        return 130

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
