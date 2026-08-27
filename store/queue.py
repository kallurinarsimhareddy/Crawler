"""A durable work queue, and the claim that makes it safe under workers.

This replaces the JSON checkpoint. The checkpoint answered "which companies are
done"; a queue also answers "which are in flight, by whom, since when, and when
may this one be tried again" — and answers it after the process has died, which
is the whole point.

**Claiming is atomic.** ``UPDATE ... WHERE state = 'pending'`` inside an
immediate transaction is what stops two workers taking one company: the second
update matches no rows, because the first already changed the state. This is
the one place correctness depends on the database rather than on the caller,
and the concurrency test exists to prove it.

**A lease, not a lock.** A worker that dies mid-crawl cannot release anything,
so a claim carries a timestamp instead and :meth:`CrawlQueue.release_stale`
returns anything held too long. A crashed run therefore costs one lease period,
not a stuck company forever.

**The interface is small on purpose.** ``enqueue``, ``claim``, ``heartbeat``,
``succeed``, ``fail``, ``release_stale``, ``stats``. A Redis-backed queue would
implement the same seven operations; nothing in the crawler would change. That
is what "prepare for a distributed queue without requiring one" means here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Final, List, Optional

from loguru import logger

from store.database import Database

__all__ = ["CrawlQueue", "DiscoveryQueue", "QueueItem", "QueueState", "DEFAULT_LEASE_SECONDS"]

#: How long a claim is honoured before another worker may take it. Long enough
#: for the slowest legitimate crawl — a browser rescue on a big board — and
#: short enough that a crash is not felt for the rest of the run.
DEFAULT_LEASE_SECONDS: Final[float] = 900.0


class QueueState(str, Enum):
    """Where a company is in its journey through a run."""

    #: Waiting to be claimed.
    PENDING = "pending"

    #: Claimed by a worker and being crawled.
    RUNNING = "running"

    #: Read successfully.
    SUCCEEDED = "succeeded"

    #: Failed in a way that is not worth retrying this run.
    FAILED = "failed"

    #: Refused by the site — a WAF, a CAPTCHA, a hard 403. Retrying is not
    #: merely useless but rude, so these are never re-claimed automatically.
    BLOCKED = "blocked"

    #: Failed transiently; claimable again once ``next_attempt_at`` passes.
    RETRY_WAIT = "retry_wait"

    #: Deliberately not crawled this run.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class QueueItem:
    """One claimed company.

    Attributes:
        company_key: The company.
        company_name: Its name, for logs.
        website: Its website.
        career_url: Its careers page.
        it_link: Its stored board, when it has one.
        platform: Its stored vendor label.
        domain: The host to rate-limit against.
        attempts: How many times it has been tried, including this one.
    """

    company_key: str
    company_name: str = ""
    website: str = ""
    career_url: str = ""
    it_link: str = ""
    platform: str = ""
    domain: str = ""
    attempts: int = 0

    def as_record(self) -> Dict[str, str]:
        """The shape :mod:`crawler.crawler_engine` consumes.

        Returns:
            A company record.
        """
        return {
            "company": self.company_name,
            "company_key": self.company_key,
            "website": self.website,
            "career_url": self.career_url,
            "it_link": self.it_link,
        }


class _Queue:
    """Shared behaviour for the crawl and discovery queues.

    Args:
        database: The store.
        table: Which queue table to operate on.
    """

    def __init__(self, database: Database, table: str) -> None:
        self.database = database
        self._table = table

    # -- filling -------------------------------------------------------------

    def enqueue_all(self, only_missing_board: bool = False, run_id: str = "") -> int:
        """Add every eligible company that is not already queued.

        Idempotent: a company already in the queue keeps whatever state it has,
        so re-running this after an interruption does not reset progress.

        Args:
            only_missing_board: Queue only companies whose ``it_link`` is
                empty. Used by discovery, never by the crawl.
            run_id: Recorded on newly queued rows.

        Returns:
            How many rows were added.
        """
        from utils.clock import iso

        condition = "AND c.it_link = ''" if only_missing_board else ""
        columns = "(company_key, state, updated_at, run_id)" if self._table == "crawl_queue" \
            else "(company_key, state, updated_at)"
        values = "(c.company_key, 'pending', ?, ?)" if self._table == "crawl_queue" \
            else "(c.company_key, 'pending', ?)"
        parameters = (iso(), run_id) if self._table == "crawl_queue" else (iso(),)

        cursor = self.database.execute(
            f"""
            INSERT INTO {self._table} {columns}
            SELECT {values.strip('()')}
              FROM companies c
             WHERE c.status IN ('active', '')
               {condition}
               AND NOT EXISTS (
                   SELECT 1 FROM {self._table} q WHERE q.company_key = c.company_key
               )
            """,
            parameters,
        )
        added = cursor.rowcount or 0
        if added:
            logger.info("{}: queued {} company(ies)", self._table, added)
        return added

    def reset_finished(self, include_blocked: bool = False) -> int:
        """Return finished companies to ``pending`` for a new run.

        ``enqueue_all`` is idempotent, which is what makes a resume safe — and
        which also means that a second week's run against last week's database
        adds nothing at all, because every company is already in the queue in a
        terminal state. Something has to say "this is a new run, try them
        again", and this is it. It is never automatic: a resume must not
        re-crawl what the interrupted segment already finished.

        ``running`` is deliberately untouched — that is the lease's business,
        and clearing it here would yank companies away from a live worker.

        Args:
            include_blocked: Also re-queue companies a site actively refused.
                Off by default: a CAPTCHA or a hard 403 is a settled answer,
                and asking again next week is a decision an operator makes
                rather than one a weekly run makes for them.

        Returns:
            How many companies were returned to ``pending``.
        """
        states = [QueueState.SUCCEEDED.value, QueueState.FAILED.value,
                  QueueState.RETRY_WAIT.value, QueueState.SKIPPED.value]
        if include_blocked:
            states.append(QueueState.BLOCKED.value)

        placeholders = ", ".join("?" for _ in states)
        cursor = self.database.execute(
            f"""
            UPDATE {self._table}
               SET state = 'pending', owner = '', claimed_at = 0,
                   attempts = 0, next_attempt_at = 0, updated_at = ?
             WHERE state IN ({placeholders})
            """,
            (_stamp(), *states),
        )
        requeued = cursor.rowcount or 0
        if requeued:
            logger.info("{}: returned {} company(ies) to pending", self._table, requeued)
        return requeued

    # -- taking work ---------------------------------------------------------

    def claim(
        self,
        owner: str,
        limit: int = 1,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> List[QueueItem]:
        """Take up to ``limit`` companies for this worker.

        The select and the update happen inside one immediate transaction, and
        the update re-checks the state it selected on. Two workers racing here
        cannot both win: the loser's ``UPDATE`` matches nothing.

        Args:
            owner: Worker name, recorded so a stuck claim can be attributed.
            limit: How many to take.
            lease_seconds: Unused here; the lease is enforced by
                :meth:`release_stale`, which the caller schedules.

        Returns:
            The claimed companies, which may be fewer than asked for.
        """
        now = time.time()
        taken: List[QueueItem] = []

        with self.database.transaction():
            candidates = self.database.query(
                f"""
                SELECT q.company_key, q.attempts,
                       c.company_name, c.website, c.career_url,
                       c.it_link, c.platform, c.domain
                  FROM {self._table} q
                  JOIN companies c ON c.company_key = q.company_key
                 WHERE (q.state = 'pending')
                    OR (q.state = 'retry_wait' AND q.next_attempt_at <= ?)
                 ORDER BY q.next_attempt_at, q.company_key
                 LIMIT ?
                """,
                (now, max(1, int(limit))),
            )

            for row in candidates:
                cursor = self.database.execute(
                    f"""
                    UPDATE {self._table}
                       SET state = 'running', owner = ?, claimed_at = ?,
                           attempts = attempts + 1, updated_at = ?
                     WHERE company_key = ?
                       AND (state = 'pending'
                            OR (state = 'retry_wait' AND next_attempt_at <= ?))
                    """,
                    (owner, now, _stamp(), row["company_key"], now),
                )
                if not cursor.rowcount:
                    # Another worker got there first. Not an error.
                    continue

                taken.append(
                    QueueItem(
                        company_key=row["company_key"],
                        company_name=row.get("company_name", ""),
                        website=row.get("website", ""),
                        career_url=row.get("career_url", ""),
                        it_link=row.get("it_link", ""),
                        platform=row.get("platform", ""),
                        domain=row.get("domain", ""),
                        attempts=int(row.get("attempts", 0)) + 1,
                    )
                )

        return taken

    def heartbeat(self, company_key: str) -> None:
        """Refresh a claim, so a long crawl is not reclaimed underneath it.

        Args:
            company_key: The company being worked on.
        """
        self.database.execute(
            f"UPDATE {self._table} SET claimed_at = ?, updated_at = ? "
            f"WHERE company_key = ? AND state = 'running'",
            (time.time(), _stamp(), company_key),
        )

    def release_stale(self, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> int:
        """Return companies whose worker went away.

        Args:
            lease_seconds: How long a claim is honoured. ``0`` releases every
                running claim, which is what a deliberate recovery wants.

        Returns:
            How many were released.
        """
        cutoff = time.time() - max(0.0, float(lease_seconds))
        cursor = self.database.execute(
            f"""
            UPDATE {self._table}
               SET state = 'pending', owner = '', claimed_at = 0, updated_at = ?
             WHERE state = 'running' AND claimed_at <= ?
            """,
            (_stamp(), cutoff),
        )
        released = cursor.rowcount or 0
        if released:
            logger.warning(
                "{}: released {} stale claim(s) back to pending", self._table, released
            )
        return released

    # -- finishing -----------------------------------------------------------

    def skip(self, company_key: str, reason: str = "") -> None:
        """Mark a company as deliberately not crawled.

        Args:
            company_key: The company.
            reason: Why.
        """
        self._set_state(company_key, QueueState.SKIPPED, reason=reason)

    def get(self, company_key: str) -> Optional[Dict[str, Any]]:
        """One queue row.

        Args:
            company_key: The company.

        Returns:
            The row, or ``None``.
        """
        return self.database.one(
            f"SELECT * FROM {self._table} WHERE company_key = ?", (company_key,)
        )

    def stats(self) -> Dict[str, int]:
        """How many companies sit in each state.

        Returns:
            State to count, including states with none.
        """
        counts = {state.value: 0 for state in QueueState}
        for row in self.database.query(
            f"SELECT state, COUNT(*) AS n FROM {self._table} GROUP BY state"
        ):
            counts[str(row["state"])] = int(row["n"])
        return counts

    def _set_state(self, company_key: str, state: QueueState, reason: str = "") -> None:
        """Move one company to a terminal state.

        Args:
            company_key: The company.
            state: Where it lands.
            reason: Recorded for the report.
        """
        self.database.execute(
            f"UPDATE {self._table} SET state = ?, owner = '', claimed_at = 0, "
            f"updated_at = ? WHERE company_key = ?",
            (state.value, _stamp(), company_key),
        )


class CrawlQueue(_Queue):
    """The weekly crawl's work queue.

    Args:
        database: The store.
    """

    def __init__(self, database: Database) -> None:
        super().__init__(database, "crawl_queue")

    def succeed(self, company_key: str, jobs: int = 0, seconds: float = 0.0,
                run_id: str = "") -> None:
        """Record a company read successfully.

        Args:
            company_key: The company.
            jobs: How many postings it yielded. Zero is a valid success — an
                empty board is a fact, not a failure.
            seconds: How long it took.
            run_id: The run.
        """
        stamp = _stamp()
        self.database.execute(
            """
            UPDATE crawl_queue
               SET state = 'succeeded', owner = '', claimed_at = 0,
                   last_attempt_at = ?, last_success_at = ?, last_reason = '',
                   jobs_found = ?, next_attempt_at = 0, updated_at = ?, run_id = ?
             WHERE company_key = ?
            """,
            (stamp, stamp, int(jobs), stamp, run_id, company_key),
        )
        self._record_attempt(company_key, "succeeded", "", jobs=jobs,
                             seconds=seconds, run_id=run_id)

    def fail(
        self,
        company_key: str,
        reason: str,
        retry_in: float,
        retryable: bool,
        http_status: int = 0,
        browser_used: bool = False,
        seconds: float = 0.0,
        run_id: str = "",
        blocked: bool = False,
    ) -> None:
        """Record a failed attempt and decide what happens next.

        Args:
            company_key: The company.
            reason: The classified failure.
            retry_in: Seconds until it may be tried again.
            retryable: Whether to try again at all.
            http_status: The status seen, when there was one.
            browser_used: Whether a browser was involved.
            seconds: How long the attempt took.
            run_id: The run.
            blocked: Whether the site actively refused us, as opposed to simply
                failing. Blocked companies are parked rather than retried.
        """
        if retryable:
            state = QueueState.RETRY_WAIT
        elif blocked:
            state = QueueState.BLOCKED
        else:
            state = QueueState.FAILED

        stamp = _stamp()
        self.database.execute(
            """
            UPDATE crawl_queue
               SET state = ?, owner = '', claimed_at = 0, last_attempt_at = ?,
                   last_reason = ?, next_attempt_at = ?, updated_at = ?, run_id = ?
             WHERE company_key = ?
            """,
            (state.value, stamp, reason, time.time() + max(0.0, retry_in),
             stamp, run_id, company_key),
        )
        self._record_attempt(company_key, state.value, reason, http_status=http_status,
                             browser_used=browser_used, seconds=seconds, run_id=run_id)

    def _record_attempt(
        self,
        company_key: str,
        outcome: str,
        reason: str,
        http_status: int = 0,
        browser_used: bool = False,
        seconds: float = 0.0,
        jobs: int = 0,
        run_id: str = "",
    ) -> None:
        """Append one attempt to the forensic log.

        Args:
            company_key: The company.
            outcome: What happened.
            reason: The classified reason, when it failed.
            http_status: The status seen.
            browser_used: Whether a browser was involved.
            seconds: Duration.
            jobs: Postings found.
            run_id: The run.
        """
        self.database.execute(
            """
            INSERT INTO crawl_attempts
                (company_key, run_id, outcome, reason, http_status,
                 browser_used, seconds, jobs_found, attempted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (company_key, run_id, outcome, reason, int(http_status),
             1 if browser_used else 0, float(seconds), int(jobs), _stamp()),
        )


class DiscoveryQueue(_Queue):
    """Board discovery's own queue, separate from the crawl's.

    Args:
        database: The store.
    """

    def __init__(self, database: Database) -> None:
        super().__init__(database, "discovery_queue")

    def resolve(self, company_key: str, url: str, platform: str,
                browser_used: bool = False) -> None:
        """Record a board found for a company.

        Args:
            company_key: The company.
            url: The board URL.
            platform: The vendor.
            browser_used: Whether the browser was needed.
        """
        self.database.execute(
            """
            UPDATE discovery_queue
               SET state = 'resolved', owner = '', claimed_at = 0,
                   found_url = ?, found_platform = ?, reason = '',
                   browser_used = ?, updated_at = ?
             WHERE company_key = ?
            """,
            (url, platform, 1 if browser_used else 0, _stamp(), company_key),
        )

    def unresolved(self, company_key: str, reason: str,
                   browser_used: bool = False) -> None:
        """Record that nothing could be confidently identified.

        Args:
            company_key: The company.
            reason: Why.
            browser_used: Whether the browser was tried.
        """
        self._finish(company_key, "unresolved", reason, browser_used)

    def refuse(self, company_key: str, reason: str, url: str = "") -> None:
        """Record a candidate found and deliberately refused.

        An aggregator, a link to one posting, a vendor's sign-in application. A
        refusal is not a failure and is never retried into a different answer.

        Args:
            company_key: The company.
            reason: Why it was refused.
            url: The candidate, kept for the report.
        """
        self.database.execute(
            """
            UPDATE discovery_queue
               SET state = 'refused', owner = '', claimed_at = 0,
                   found_url = ?, reason = ?, updated_at = ?
             WHERE company_key = ?
            """,
            (url, reason, _stamp(), company_key),
        )

    def block(self, company_key: str, reason: str) -> None:
        """Record a site that could not be read at all.

        Args:
            company_key: The company.
            reason: The blocker.
        """
        self._finish(company_key, "blocked", reason, False)

    def _finish(self, company_key: str, state: str, reason: str,
                browser_used: bool) -> None:
        """Move a discovery row to a terminal state.

        Args:
            company_key: The company.
            state: Where it lands.
            reason: Why.
            browser_used: Whether the browser was tried.
        """
        self.database.execute(
            """
            UPDATE discovery_queue
               SET state = ?, owner = '', claimed_at = 0, reason = ?,
                   browser_used = ?, updated_at = ?
             WHERE company_key = ?
            """,
            (state, reason, 1 if browser_used else 0, _stamp(), company_key),
        )


def _stamp() -> str:
    """The current time, as the sheet writes it.

    Returns:
        An ISO timestamp.
    """
    from utils.clock import iso

    return iso()
