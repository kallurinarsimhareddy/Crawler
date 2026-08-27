"""Job storage: the current view, the history behind it, and the weekly diff.

Three tabs, and the relationship between them is the whole design.

``CURRENT_JOBS`` is a **snapshot**: what is open right now, replaced wholesale
each run. ``JOB_HISTORY`` is a **ledger**: one row per posting ever seen,
updated in place, never re-added. ``NEW_LAST_WEEK`` is an **append-only log** of
what changed, one block per run.

    >>> from sheets.jobs import JobRepository
    >>> jobs = JobRepository(client)
    >>> changes = jobs.apply(observations, crawled_company_keys, run_id="2026-W35-...")
    >>> changes.summary()["jobs_new"]
    143

**Dates come from observation, not from the board.** ``First Seen`` is stamped
when this crawler first saw a posting and never moves again. That is deliberate:
most applicant tracking systems publish no posted date at all, and of those that
do, some report when the requisition was created, some when it was last edited,
and some when it was last indexed. None are comparable across eight thousand
companies. ``Posted Date`` is still stored when a board volunteers one, but
nothing is ever inferred from it.

**A closure needs evidence.** :meth:`JobRepository.apply` closes a posting only
when its company was successfully read on this run. On the reference sheet 389
of 8,275 companies fail on a given Friday; closing their jobs would report
roughly eleven thousand closures on a quiet week, then report the same jobs as
new again the following week when the boards came back. The rule lives in
:func:`crawler.weekly_diff.compare` and is enforced here by passing it the set
of companies actually read.

**Reruns are safe.** The weekly log is keyed on the run, so running the same
run twice appends its changes once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from loguru import logger

from crawler.weekly_diff import (
    STATUS_ACTIVE,
    STATUS_CLOSED,
    KnownJob,
    ObservedJob,
    WeeklyChanges,
    compare,
)
from sheets.client import SheetsClient
from sheets.schema import CURRENT_JOBS, JOB_HISTORY, NEW_LAST_WEEK
from sheets.storage import Record, TabStore, UpsertResult
from utils.clock import iso, week_of

__all__ = [
    "CHANGE_CLOSED",
    "CHANGE_NEW",
    "CHANGE_REOPENED",
    "AppliedChanges",
    "JobRepository",
    "observation",
]

#: Values a sheet cell can hold that mean "no". Everything read back from a
#: spreadsheet is a string, so a stored ``FALSE`` arrives as text and must not
#: be mistaken for a truthy value.
_FALSEY: frozenset = frozenset({"", "false", "0", "no", "n", "none", "null"})


def _is_true(value: object) -> bool:
    """Interpret a flag that may be a bool or a string from a cell.

    Args:
        value: What the observation or the sheet supplied.

    Returns:
        ``True`` unless the value is falsey in either form.
    """
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in _FALSEY


#: Change labels written to ``NEW_LAST_WEEK``.
CHANGE_NEW: str = "new"
CHANGE_REOPENED: str = "reopened"
CHANGE_CLOSED: str = "closed"

#: Fields carried from an observation into ``JOB_HISTORY``.
_HISTORY_FIELDS: Tuple[str, ...] = (
    "company_name",
    "company_key",
    "job_title",
    "job_url",
    "url_key",
    "content_key",
    "platform",
    "department",
    "location",
    "country",
)

#: Fields carried from an observation into ``CURRENT_JOBS``.
_CURRENT_FIELDS: Tuple[str, ...] = (
    "company_name",
    "job_title",
    "job_url",
    "career_url",
    "platform",
    "department",
    "location",
    "country",
    "workplace_type",
    "employment_type",
    "posted_date",
    "job_id",
    "industry",
    "source",
)


def observation(
    job_key: str,
    company_key: str,
    company_name: str = "",
    job_title: str = "",
    job_url: str = "",
    url_key: str = "",
    content_key: str = "",
    **extra: object,
) -> Dict[str, str]:
    """Build one observation, the shape :meth:`JobRepository.apply` consumes.

    Args:
        job_key: The posting's stable identity, from :mod:`crawler.identity`.
        company_key: The company it belongs to.
        company_name: The company as named.
        job_title: The posting title.
        job_url: Its URL.
        url_key: The canonical URL, for re-linking when the identity moves.
        content_key: Company/title/location hash, the last-resort re-link.
        **extra: Any other stored field — ``platform``, ``location``,
            ``department``, ``posted_date`` and the rest.

    Returns:
        The observation.
    """
    record: Dict[str, str] = {
        "job_key": job_key,
        "company_key": company_key,
        "company_name": company_name,
        "job_title": job_title,
        "job_url": job_url,
        "url_key": url_key,
        "content_key": content_key,
    }
    # Booleans are kept as booleans. Stringifying them turns False into the
    # string "False", which is truthy -- and the technology filter, which reads
    # is_tech, would then admit every posting.
    record.update(
        {
            name: value if isinstance(value, bool) else ("" if value is None else str(value))
            for name, value in extra.items()
        }
    )
    return record


@dataclass
class AppliedChanges:
    """What writing a week's comparison did.

    Attributes:
        changes: The comparison itself.
        history: What the ledger write did.
        current: What the snapshot write did.
        weekly: What the change log write did.
        run_id: The run these belong to.
        already_logged: Whether this run's changes were already in the log, so
            the append was skipped.
    """

    changes: WeeklyChanges
    history: UpsertResult = field(default_factory=UpsertResult)
    current: UpsertResult = field(default_factory=UpsertResult)
    weekly: UpsertResult = field(default_factory=UpsertResult)
    run_id: str = ""
    already_logged: bool = False

    def summary(self) -> Dict[str, int]:
        """Render the whole operation as counts.

        Returns:
            Metric name to value.
        """
        totals = dict(self.changes.summary())
        totals.update(
            {
                "history_inserted": self.history.inserted,
                "history_updated": self.history.updated,
                "current_rows": self.current.inserted + self.current.updated + self.current.unchanged,
                "weekly_rows": self.weekly.inserted,
            }
        )
        return totals

    def describe(self) -> str:
        """Render as one line for a report.

        Returns:
            Human-readable summary.
        """
        counts = self.changes.summary()
        return (
            f"{counts['jobs_new']} new, {counts['jobs_reopened']} reopened, "
            f"{counts['jobs_closed']} closed, {counts['jobs_still_active']} unchanged"
            + (" (already logged)" if self.already_logged else "")
        )


class JobRepository:
    """Reads and writes ``JOB_HISTORY``, ``CURRENT_JOBS`` and ``NEW_LAST_WEEK``.

    Args:
        client: The Sheets client.
        history_title: Live title of the history tab, if not the default.
        current_title: Live title of the snapshot tab, if not the default.
        weekly_title: Live title of the change log, if not the default.
    """

    def __init__(
        self,
        client: SheetsClient,
        history_title: Optional[str] = None,
        current_title: Optional[str] = None,
        weekly_title: Optional[str] = None,
    ) -> None:
        self.history = TabStore(client, JOB_HISTORY, history_title)
        self.current = TabStore(client, CURRENT_JOBS, current_title)
        self.weekly = TabStore(client, NEW_LAST_WEEK, weekly_title)

    # -- reading -------------------------------------------------------------

    def known_jobs(self) -> List[KnownJob]:
        """Every posting the ledger holds, in the shape the comparison wants.

        Returns:
            The jobs, active and closed.
        """
        return [
            KnownJob(
                job_uid=record.get("job_key"),
                company_key=record.get("company_key"),
                url_key=record.get("url_key"),
                content_key=record.get("content_key"),
                status=record.get("status", STATUS_ACTIVE) or STATUS_ACTIVE,
                title=record.get("job_title"),
                first_seen=record.get("first_seen"),
            )
            for record in self.history.read()
            if record.get("job_key")
        ]

    def active_count(self) -> int:
        """How many postings the ledger marks open.

        Returns:
            The count.
        """
        return sum(
            1
            for record in self.history.read()
            if (record.get("status") or STATUS_ACTIVE) == STATUS_ACTIVE
        )

    def logged_runs(self) -> Set[str]:
        """Which runs have already written to the change log.

        Returns:
            Their run identifiers. This is what makes a rerun append once.
        """
        return {
            record.get("run_id")
            for record in self.weekly.read()
            if record.get("run_id")
        }

    # -- writing -------------------------------------------------------------

    def apply(
        self,
        observations: Sequence[Mapping[str, object]],
        crawled: Set[str],
        run_id: str,
        tech_only: bool = True,
        dry_run: bool = False,
    ) -> AppliedChanges:
        """Compare this run against the ledger and write the result everywhere.

        Args:
            observations: Every posting this run saw, as :func:`observation`
                produces. Already deduplicated by the crawler.
            crawled: ``company_key`` for every company **successfully read** on
                this run — including those whose board turned out to be empty,
                and excluding every company that failed, was blocked or was not
                attempted. Postings belonging to a company outside this set are
                never closed.
            run_id: The run these changes belong to.
            tech_only: Whether ``CURRENT_JOBS`` should hold technology roles
                only. ``JOB_HISTORY`` always keeps every posting, so the filter
                can be changed later without re-crawling; only the current view
                is narrowed.
            dry_run: Work out what would change, and write nothing.

        Returns:
            The comparison and what was written.
        """
        # One read of the ledger, used twice. known_jobs() would read it again,
        # and at a hundred thousand rows against a sixty-reads-per-minute quota
        # a redundant full-tab read is not a rounding error.
        stored = [record for record in self.history.read() if record.get("job_key")]
        by_key = {record.get("job_key"): record for record in stored}
        known = [
            KnownJob(
                job_uid=record.get("job_key"),
                company_key=record.get("company_key"),
                url_key=record.get("url_key"),
                content_key=record.get("content_key"),
                status=record.get("status", STATUS_ACTIVE) or STATUS_ACTIVE,
                title=record.get("job_title"),
                first_seen=record.get("first_seen"),
            )
            for record in stored
        ]

        seen = [
            ObservedJob(
                job_uid=str(item.get("job_key") or ""),
                company_key=str(item.get("company_key") or ""),
                url_key=str(item.get("url_key") or ""),
                content_key=str(item.get("content_key") or ""),
                title=str(item.get("job_title") or ""),
            )
            for item in observations
            if item.get("job_key")
        ]

        changes = compare(previous=known, observed=seen, crawled=crawled)
        detail = {str(item.get("job_key")): item for item in observations if item.get("job_key")}

        applied = AppliedChanges(changes=changes, run_id=run_id)
        stamp = iso()

        applied.history = self._write_history(changes, detail, by_key, run_id, stamp, dry_run)
        applied.current = self._write_current(
            changes, detail, by_key, run_id, stamp, dry_run, tech_only
        )
        applied.weekly, applied.already_logged = self._write_weekly(
            changes, detail, by_key, run_id, stamp, dry_run
        )

        logger.success("Weekly changes for {}: {}", run_id, applied.describe())
        return applied

    def _write_history(
        self,
        changes: WeeklyChanges,
        detail: Mapping[str, Mapping[str, object]],
        existing: Mapping[str, Record],
        run_id: str,
        stamp: str,
        dry_run: bool,
    ) -> UpsertResult:
        """Update the ledger.

        Args:
            changes: The comparison.
            detail: Observation detail by job key.
            existing: Ledger rows by job key, as they were before this run.
            run_id: The run.
            stamp: Its timestamp.
            dry_run: Write nothing.

        Returns:
            What was done.
        """
        records: List[Dict[str, str]] = []

        # A re-link carries a posting onto the identity this run derived, so it
        # is written under the new key while keeping the old row's dates.
        relinked: Dict[str, Record] = {}
        for relink in changes.relinked:
            previous = existing.get(relink.previous_uid)
            if previous is not None:
                relinked[relink.observed.job_uid] = previous

        def carry(job_key: str) -> Dict[str, str]:
            """Build the ledger row for one observed posting."""
            item = detail.get(job_key, {})
            prior = existing.get(job_key) or relinked.get(job_key)

            record: Dict[str, str] = {"job_key": job_key}
            for name in _HISTORY_FIELDS:
                record[name] = str(item.get(name) or "")

            record["status"] = STATUS_ACTIVE
            record["closed_at"] = ""
            record["last_run_id"] = run_id

            # Replaying a run must land on the state that run already produced,
            # so Last Seen is only advanced when this is genuinely a later run.
            # Without this a rerun rewrites every row with a new timestamp, and
            # "safe to re-run" -- the only recovery an interrupted weekly run
            # has -- would cost a full rewrite every time.
            if prior is not None and prior.get("last_run_id") == run_id and prior.get("last_seen"):
                record["last_seen"] = prior.get("last_seen")
            else:
                record["last_seen"] = stamp

            # First Seen and First Run are written once and never again. A
            # board that re-publishes a posting under a new URL must not be
            # able to reset how long it has been open.
            if prior is not None and prior.get("first_seen"):
                record["first_seen"] = prior.get("first_seen")
                record["first_run_id"] = prior.get("first_run_id")
            else:
                record["first_seen"] = stamp
                record["first_run_id"] = run_id

            return record

        for job in changes.new_jobs:
            records.append(carry(job.job_uid))
        for job in changes.reopened_jobs:
            records.append(carry(job.job_uid))
        for job in changes.still_active:
            records.append(carry(job.job_uid))

        for job in changes.closed_jobs:
            prior = existing.get(job.job_uid)
            records.append(
                {
                    "job_key": job.job_uid,
                    "status": STATUS_CLOSED,
                    # Stamped only on the run that observed the closure, so a
                    # job closed weeks ago keeps the date it actually closed.
                    "closed_at": (prior.get("closed_at") if prior else "") or stamp,
                    "last_run_id": run_id,
                }
            )

        # A re-linked posting's old row is rewritten under the new key, so the
        # stale key must not be left behind claiming to be active.
        for relink in changes.relinked:
            if relink.previous_uid in existing:
                records.append(
                    {
                        "job_key": relink.previous_uid,
                        "status": STATUS_CLOSED,
                        "closed_at": stamp,
                        "last_run_id": run_id,
                    }
                )

        return self.history.upsert(
            records,
            key_field="job_key",
            # A reopened posting must lose its Closed At, and the default rule
            # -- a blank never clears a stored value -- would keep it forever.
            clearable=frozenset({"closed_at"}),
            # First Seen and First Run are the dates the whole "new this week"
            # question rests on. They are written when a posting first appears
            # and are not the crawler's to revise afterwards.
            insert_only=frozenset({"first_seen", "first_run_id"}),
            dry_run=dry_run,
        )

    def _write_current(
        self,
        changes: WeeklyChanges,
        detail: Mapping[str, Mapping[str, object]],
        existing: Mapping[str, Record],
        run_id: str,
        stamp: str,
        dry_run: bool,
        tech_only: bool = True,
    ) -> UpsertResult:
        """Rewrite the snapshot of what is open now.

        Args:
            changes: The comparison.
            detail: Observation detail by job key.
            existing: Ledger rows by job key, for their First Seen dates.
            run_id: The run.
            stamp: Its timestamp.
            dry_run: Write nothing.
            tech_only: Keep only postings the observation flagged as technical.

        Returns:
            What was done.
        """
        rows: List[Dict[str, str]] = []
        filtered = 0

        for job in [*changes.new_jobs, *changes.reopened_jobs, *changes.still_active]:
            item = detail.get(job.job_uid, {})
            prior = existing.get(job.job_uid)

            # The narrowing that keeps this tab readable. On a real run it
            # takes 237,300 postings down to about 17,800: the rest are
            # manufacturing, clinical, warehouse and retail roles, which the
            # ledger still records in full.
            if tech_only and not _is_true(item.get("is_tech")):
                filtered += 1
                continue

            record: Dict[str, str] = {"job_key": job.job_uid}
            for name in _CURRENT_FIELDS:
                record[name] = str(item.get(name) or "")

            record["first_seen"] = (prior.get("first_seen") if prior else "") or stamp
            record["run_id"] = run_id

            # Mirrors the ledger: replaying a run reproduces that run's rows
            # rather than rewriting the whole snapshot with a fresh timestamp.
            if prior is not None and prior.get("last_run_id") == run_id and prior.get("last_seen"):
                record["last_seen"] = prior.get("last_seen")
            else:
                record["last_seen"] = stamp

            rows.append(record)

        if filtered:
            logger.info(
                "CURRENT_JOBS: {} technology posting(s); {} non-technology held in JOB_HISTORY",
                len(rows),
                filtered,
            )

        rows.sort(key=lambda record: (record.get("company_name", ""), record.get("job_title", "")))
        return self.current.replace(rows, dry_run=dry_run)

    def _write_weekly(
        self,
        changes: WeeklyChanges,
        detail: Mapping[str, Mapping[str, object]],
        existing: Mapping[str, Record],
        run_id: str,
        stamp: str,
        dry_run: bool,
    ) -> Tuple[UpsertResult, bool]:
        """Append this run's changes to the weekly log, exactly once.

        Args:
            changes: The comparison.
            detail: Observation detail by job key.
            existing: Ledger rows by job key.
            run_id: The run.
            stamp: Its timestamp.
            dry_run: Write nothing.

        Returns:
            ``(result, already_logged)``.
        """
        if run_id and run_id in self.logged_runs():
            logger.info("Run {} is already in the weekly log; not appending again", run_id)
            return UpsertResult(dry_run=dry_run), True

        week_start, week_end = week_of()
        rows: List[Dict[str, str]] = []

        def entry(job_key: str, change: str, fallback_title: str = "") -> Dict[str, str]:
            """Build one change-log row."""
            item = detail.get(job_key, {})
            prior = existing.get(job_key)

            return {
                "week_start": week_start,
                "week_end": week_end,
                "change": change,
                "company_name": str(item.get("company_name") or (prior.get("company_name") if prior else "")),
                "job_title": str(item.get("job_title") or fallback_title or (prior.get("job_title") if prior else "")),
                "job_url": str(item.get("job_url") or (prior.get("job_url") if prior else "")),
                "platform": str(item.get("platform") or (prior.get("platform") if prior else "")),
                "department": str(item.get("department") or (prior.get("department") if prior else "")),
                "location": str(item.get("location") or (prior.get("location") if prior else "")),
                "country": str(item.get("country") or (prior.get("country") if prior else "")),
                "industry": str(item.get("industry") or ""),
                "first_seen": (prior.get("first_seen") if prior else "") or stamp,
                "last_seen": stamp,
                "run_id": run_id,
                "job_key": job_key,
            }

        for job in changes.new_jobs:
            rows.append(entry(job.job_uid, CHANGE_NEW))
        for job in changes.reopened_jobs:
            rows.append(entry(job.job_uid, CHANGE_REOPENED))
        for job in changes.closed_jobs:
            rows.append(entry(job.job_uid, CHANGE_CLOSED, fallback_title=job.title))

        if not rows:
            return UpsertResult(dry_run=dry_run), False

        return self.weekly.append(rows, dry_run=dry_run), False
