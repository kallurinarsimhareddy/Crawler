"""Run bookkeeping: ``WEEKLY_RUNS``, ``FAILURES`` and ``DASHBOARD``.

Three tabs with three different lifetimes, and the differences are the point.

``WEEKLY_RUNS`` is **permanent**: one row per run, updated in place as the run
progresses and closed when it ends. This is where week-over-week history lives,
which is why ``FAILURES`` does not need to keep any.

``FAILURES`` is the **latest run only**. A weekly run produces a few hundred
failures; appending them forever would add a hundred thousand rows a year to say
what ``WEEKLY_RUNS`` already records as counts. Each run replaces the tab with
its own failures, keyed on company so a rerun of the same run updates rather
than duplicates.

``DASHBOARD`` is **derived**: rewritten from the other tabs each run, holding
current totals and a week-by-week block beneath them.

    >>> from sheets.runs import RunRepository
    >>> runs = RunRepository(client)
    >>> run = runs.start(companies_total=8275)
    >>> runs.finish(run.run_id, status="done", counts={"jobs_new": 143})

A run identifier carries the ISO week it belongs to — ``2026-W35-20260828T060000Z``
— so runs sort chronologically, a second run in one week does not collide with
the first, and a glance at a log file says which run wrote it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from sheets.client import SheetsClient
from sheets.schema import DASHBOARD, FAILURES, WEEKLY_RUNS
from sheets.storage import Record, TabStore, UpsertResult
from utils.blocking import Block, classify_text, is_retryable
from utils.clock import iso, parse, run_id_for, utc_now, week_of

__all__ = [
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_INTERRUPTED",
    "STATUS_RUNNING",
    "DashboardRepository",
    "FailureRepository",
    "RunRecord",
    "RunRepository",
    "failure_record",
]

#: A run that is still going, or that died without saying otherwise.
STATUS_RUNNING: str = "running"

#: A run that finished its work.
STATUS_DONE: str = "done"

#: A run stopped deliberately — Ctrl-C, or a shutdown signal.
STATUS_INTERRUPTED: str = "interrupted"

#: A run that stopped on an error it could not isolate.
STATUS_FAILED: str = "failed"

#: The counting columns a run maintains, so a caller can pass any subset.
COUNT_FIELDS: Tuple[str, ...] = (
    "companies_total",
    "companies_checked",
    "companies_succeeded",
    "companies_failed",
    "companies_with_jobs",
    "companies_no_jobs",
    "jobs_active",
    "jobs_new",
    "jobs_closed",
    "companies_discovered",
)


@dataclass
class RunRecord:
    """One row of ``WEEKLY_RUNS``.

    Attributes:
        run_id: Its identifier.
        week_start: Monday of the ISO week it belongs to.
        week_end: The Sunday after.
        started_at: When it began.
        finished_at: When it ended, or ``""`` while running.
        status: One of the module's status constants.
        mode: What it was asked to do.
        counts: The counting columns.
        notes: Free text.
    """

    run_id: str
    week_start: str = ""
    week_end: str = ""
    started_at: str = ""
    finished_at: str = ""
    status: str = STATUS_RUNNING
    mode: str = "weekly"
    counts: Dict[str, int] = field(default_factory=dict)
    notes: str = ""

    @property
    def is_finished(self) -> bool:
        """Whether the run reached a terminal state.

        Returns:
            ``True`` unless it is still marked running.
        """
        return self.status != STATUS_RUNNING


def _elapsed(started: str, finished: str) -> str:
    """Render the gap between two timestamps as ``1h 04m 09s``.

    Args:
        started: ISO-8601 start.
        finished: ISO-8601 end.

    Returns:
        The duration, or ``""`` when either timestamp is unusable.
    """
    first, second = parse(started), parse(finished)
    if first is None or second is None:
        return ""

    seconds = max(0, int((second - first).total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, remainder = divmod(rest, 60)

    if hours:
        return f"{hours}h {minutes:02d}m {remainder:02d}s"
    if minutes:
        return f"{minutes}m {remainder:02d}s"
    return f"{remainder}s"


class RunRepository:
    """Reads and writes ``WEEKLY_RUNS``.

    Args:
        client: The Sheets client.
        title: The live tab's title, if not the default.
    """

    def __init__(self, client: SheetsClient, title: Optional[str] = None) -> None:
        self.store = TabStore(client, WEEKLY_RUNS, title)

    def start(
        self,
        companies_total: int = 0,
        mode: str = "weekly",
        run_id: Optional[str] = None,
        notes: str = "",
        dry_run: bool = False,
    ) -> RunRecord:
        """Open a run.

        Args:
            companies_total: How many companies it intends to crawl.
            mode: What it was asked to do.
            run_id: Override the generated identifier. Passing an existing one
                resumes that run's row rather than creating a second.
            notes: Free text.
            dry_run: Write nothing.

        Returns:
            The run.
        """
        started = utc_now()
        identifier = run_id or run_id_for(started)
        week_start, week_end = week_of(started)

        existing = self.get(identifier)

        # A run that already completed is not reopened. The weekly runner mints
        # a fresh identifier each week, so reaching here with a finished run
        # means a deliberate replay -- and a replay should converge on the state
        # that run already produced rather than restart it and re-time it. An
        # *interrupted* run is a different matter: its status is not `done`, so
        # it falls through and is resumed.
        if existing is not None and existing.status == STATUS_DONE:
            logger.info("Run {} already completed; not restarting it", identifier)
            return existing
        record = RunRecord(
            run_id=identifier,
            week_start=week_start,
            week_end=week_end,
            # A resumed run keeps the moment it originally began, so its
            # duration reflects the whole thing rather than the last attempt.
            started_at=(existing.started_at if existing else "") or iso(started),
            status=STATUS_RUNNING,
            mode=mode,
            counts={"companies_total": int(companies_total)},
            notes=notes,
        )

        self._write(record, dry_run=dry_run)
        logger.info("Run {} started ({}, {} companies)", identifier, mode, companies_total)
        return record

    def finish(
        self,
        run_id: str,
        status: str = STATUS_DONE,
        counts: Optional[Mapping[str, int]] = None,
        notes: Optional[str] = None,
        dry_run: bool = False,
    ) -> UpsertResult:
        """Close a run.

        Args:
            run_id: Its identifier.
            status: Terminal status to record.
            counts: Final counts, any subset of :data:`COUNT_FIELDS`.
            notes: Free text to record.
            dry_run: Write nothing.

        Returns:
            What was written.
        """
        existing = self.get(run_id)
        started = existing.started_at if existing else ""

        # Closing a run that already closed the same way keeps its original end
        # time. A run finished when it finished; re-stamping it on a replay
        # would inflate the recorded duration by however long the replay was
        # deferred, and would make an otherwise identical rerun a write.
        if existing is not None and existing.finished_at and existing.status == status:
            finished = existing.finished_at
        else:
            finished = iso()

        record: Dict[str, Any] = {
            "run_id": run_id,
            "finished_at": finished,
            "duration": _elapsed(started, finished),
            "status": status,
        }

        merged = dict(existing.counts) if existing else {}
        merged.update({name: int(value) for name, value in (counts or {}).items()})
        record.update({name: str(value) for name, value in merged.items()})

        checked = merged.get("companies_checked", 0)
        succeeded = merged.get("companies_succeeded", 0)
        if checked:
            record["success_rate"] = f"{succeeded / checked * 100:.1f}%"

        if notes is not None:
            record["notes"] = notes

        result = self.store.upsert([record], key_field="run_id", dry_run=dry_run)
        logger.info("Run {} finished: {}", run_id, status)
        return result

    def update_counts(
        self,
        run_id: str,
        counts: Mapping[str, int],
        dry_run: bool = False,
    ) -> UpsertResult:
        """Refresh a running run's counters.

        Args:
            run_id: Its identifier.
            counts: Any subset of :data:`COUNT_FIELDS`.
            dry_run: Write nothing.

        Returns:
            What was written.
        """
        record: Dict[str, Any] = {"run_id": run_id}
        record.update({name: str(int(value)) for name, value in counts.items()})
        return self.store.upsert([record], key_field="run_id", dry_run=dry_run)

    def _write(self, record: RunRecord, dry_run: bool) -> UpsertResult:
        """Write a run row.

        Args:
            record: The run.
            dry_run: Write nothing.

        Returns:
            What was written.
        """
        values: Dict[str, Any] = {
            "run_id": record.run_id,
            "week_start": record.week_start,
            "week_end": record.week_end,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "status": record.status,
            "mode": record.mode,
            "notes": record.notes,
        }
        values.update({name: str(value) for name, value in record.counts.items()})
        return self.store.upsert([values], key_field="run_id", dry_run=dry_run)

    def get(self, run_id: str) -> Optional[RunRecord]:
        """Read one run.

        Args:
            run_id: Its identifier.

        Returns:
            The run, or ``None``.
        """
        record = self.store.read_index("run_id").get(run_id)
        return self._to_record(record) if record is not None else None

    def all(self) -> List[RunRecord]:
        """Every run, in sheet order.

        Returns:
            The runs.
        """
        return [self._to_record(record) for record in self.store.read() if record.get("run_id")]

    def last_completed(self) -> Optional[RunRecord]:
        """The most recent run that actually finished.

        This is the baseline a weekly comparison measures against. An
        interrupted run saw only part of the world, and diffing against it would
        report everything it missed as newly closed.

        Returns:
            The run, or ``None``.
        """
        finished = [run for run in self.all() if run.status == STATUS_DONE]
        if not finished:
            return None
        return max(finished, key=lambda run: run.started_at)

    def resumable(self) -> Optional[RunRecord]:
        """An unfinished run from this week, if there is one.

        Returns:
            The run to resume, or ``None``. Runs from an earlier week are not
            offered: their postings would be filed under the wrong week and
            compared against the wrong baseline.
        """
        week_start, _ = week_of()
        candidates = [
            run
            for run in self.all()
            if run.week_start == week_start and run.status in (STATUS_RUNNING, STATUS_INTERRUPTED)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda run: run.started_at)

    @staticmethod
    def _to_record(record: Record) -> RunRecord:
        """Build a :class:`RunRecord` from a sheet row.

        Args:
            record: The row.

        Returns:
            The run.
        """
        counts: Dict[str, int] = {}
        for name in COUNT_FIELDS:
            raw = record.get(name)
            if raw:
                try:
                    counts[name] = int(float(raw))
                except ValueError:
                    continue

        return RunRecord(
            run_id=record.get("run_id"),
            week_start=record.get("week_start"),
            week_end=record.get("week_end"),
            started_at=record.get("started_at"),
            finished_at=record.get("finished_at"),
            status=record.get("status", STATUS_RUNNING) or STATUS_RUNNING,
            mode=record.get("mode", "weekly") or "weekly",
            counts=counts,
            notes=record.get("notes"),
        )


def failure_record(
    company_key: str,
    company_name: str = "",
    website: str = "",
    crawled_url: str = "",
    platform: str = "",
    error: str = "",
    run_id: str = "",
) -> Dict[str, str]:
    """Classify one company's failure into a ``FAILURES`` row.

    The classification comes from :mod:`utils.blocking`, which distinguishes the
    cases that need different work: a bad URL in the sheet needs an edit, a
    CAPTCHA needs nothing at all, and a 429 needs only patience.

    Args:
        company_key: The company's identity.
        company_name: Its name.
        website: Its website.
        crawled_url: The URL that was actually attempted.
        platform: The ATS detected, if any.
        error: The recorded error message.
        run_id: The run.

    Returns:
        The record.
    """
    block = classify_text(error)

    status = ""
    if block in (Block.FORBIDDEN, Block.NOT_FOUND, Block.RATE_LIMITED):
        status = block.value.split()[0]

    return {
        "company_key": company_key,
        "company_name": company_name,
        "website": website,
        "crawled_url": crawled_url,
        "platform": platform,
        "failure_type": block.value,
        "http_status": status,
        # Trimmed: the full body of a Cloudflare interstitial is several
        # kilobytes, and a cell holding it is unreadable and expensive.
        "detail": (error or "")[:500],
        "browser_required": "TRUE" if block is Block.BROWSER_REQUIRED else "FALSE",
        "retryable": "TRUE" if is_retryable(block) else "FALSE",
        "run_id": run_id,
        "checked_at": iso(),
    }


class FailureRepository:
    """Reads and writes ``FAILURES``.

    Args:
        client: The Sheets client.
        title: The live tab's title, if not the default.
    """

    def __init__(self, client: SheetsClient, title: Optional[str] = None) -> None:
        self.store = TabStore(client, FAILURES, title)

    def replace(
        self,
        records: Iterable[Mapping[str, object]],
        dry_run: bool = False,
    ) -> UpsertResult:
        """Make the tab exactly this run's failures.

        Args:
            records: Failure records, as :func:`failure_record` produces.
            dry_run: Write nothing.

        Returns:
            What was written.
        """
        rows = list(records)
        rows.sort(key=lambda record: (str(record.get("failure_type", "")), str(record.get("company_name", ""))))
        return self.store.replace(rows, dry_run=dry_run)

    def upsert(
        self,
        records: Iterable[Mapping[str, object]],
        dry_run: bool = False,
    ) -> UpsertResult:
        """Add or refresh failures without clearing the rest.

        Used while a run is in progress, so the tab is useful before the run
        ends. Keyed on company, so a company that fails twice in one run
        occupies one row rather than two.

        Args:
            records: Failure records.
            dry_run: Write nothing.

        Returns:
            What was written.
        """
        return self.store.upsert(records, key_field="company_key", dry_run=dry_run)

    def resolve(self, company_keys: Iterable[str], dry_run: bool = False) -> int:
        """Take companies out of ``FAILURES`` because they have since been read.

        The tab reports *current unresolved* failures -- its own specification
        says it is replaced each run, and the historical counts live in
        ``WEEKLY_RUNS``. An incremental run cannot keep that promise by
        upserting alone: it adds the companies that failed and never revisits
        the ones that stopped failing, so a company fixed in March is still
        accusing itself in June.

        Removing the row loses nothing an operator needs. ``WEEKLY_RUNS``
        retains this run's failure count, the run log retains the error, and a
        company that fails again is simply written again.

        Args:
            company_keys: Companies **successfully read**. A company that was
                not attempted this run must not be offered here: its failure is
                unresolved, not resolved.
            dry_run: Work out what would go, and write nothing.

        Returns:
            How many stale failure rows were removed.
        """
        return self.store.remove(company_keys, key_field="company_key", dry_run=dry_run)

    def counts_by_type(self) -> Dict[str, int]:
        """How many companies each failure type accounts for.

        Returns:
            Failure type to company count, largest first.
        """
        counts: Dict[str, int] = {}
        for record in self.store.read():
            label = record.get("failure_type")
            if label:
                counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: -item[1]))

    def count(self) -> int:
        """How many failures the tab holds.

        Returns:
            The count.
        """
        return self.store.count()


class DashboardRepository:
    """Writes ``DASHBOARD``.

    The data layer only: metrics as rows, no formulas and no charts. A chart
    added by hand sits above or beside this block and is not disturbed, because
    :meth:`sheets.storage.TabStore.replace` writes only the columns the crawler
    manages.

    Args:
        client: The Sheets client.
        title: The live tab's title, if not the default.
    """

    def __init__(self, client: SheetsClient, title: Optional[str] = None) -> None:
        self.store = TabStore(client, DASHBOARD, title)

    def write(
        self,
        sections: Sequence[Tuple[str, Sequence[Tuple[str, object]]]],
        week_start: str = "",
        dry_run: bool = False,
    ) -> UpsertResult:
        """Rewrite the dashboard.

        Args:
            sections: ``(section, [(metric, value), ...])`` in display order.
            week_start: The week these figures describe.
            dry_run: Write nothing.

        Returns:
            What was written.
        """
        stamp = iso()
        week = week_start or week_of()[0]

        # "Updated" means when this figure last *changed*, not when the run last
        # looked. Stamping every row on every write would make an unchanged
        # dashboard a full rewrite each week — and would mean a metric's
        # timestamp told the reader nothing about the metric.
        previous = {
            (record.get("section"), record.get("metric")): record
            for record in self.store.read()
        }

        rows = []
        for section, metrics in sections:
            for metric, value in metrics:
                rendered = "" if value is None else str(value)
                prior = previous.get((section, metric))

                unchanged = (
                    prior is not None
                    and prior.get("value") == rendered
                    and prior.get("week_start") == week
                )

                rows.append(
                    {
                        "section": section,
                        "metric": metric,
                        "value": rendered,
                        "week_start": week,
                        "updated_at": prior.get("updated_at") if unchanged else stamp,
                    }
                )

        return self.store.replace(rows, dry_run=dry_run)

    def read_metrics(self) -> Dict[str, str]:
        """Read the dashboard back as metric to value.

        Returns:
            The metrics. Where a metric appears in several sections the last
            wins, which is the display order a reader would expect.
        """
        return {
            record.get("metric"): record.get("value")
            for record in self.store.read()
            if record.get("metric")
        }
