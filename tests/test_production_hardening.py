"""The five things a full-scale run proved were wrong, and what now holds.

Each group here defends one property that a twelve-thousand-company run showed
was missing. None of them are theoretical: every one was observed against the
real roster before it was fixed.

**A server may not park a worker for an hour.** ``Retry-After`` is honoured, as
it must be, but a header is a number the other side chooses and urllib3's own
ceiling for it is six hours. One of six workers asleep on a single response
costs a sixth of the run.

**``CURRENT_JOBS`` must mean "open now".** Incremental writing upserts and never
revisits, so a posting that closes is written once and never touched again. The
tab slowly stops being a snapshot and becomes an archive nobody asked for.

**``FAILURES`` must mean "unresolved now".** Same mechanism, same drift: a
company that failed in March and has succeeded every week since is still
accusing itself.

**``IT_KEYWORDS`` must actually decide something.** The tab, the loader and the
seventy seeded terms all existed; nothing in the production path called any of
them, so editing the spreadsheet changed nothing at all.

**A removal must not corrupt the tab it tidies.** :meth:`TabStore.read` skips a
wholly blank row and appends land at ``FIRST_DATA_ROW + len(read())``, so a
blank punched into the middle of a tab makes the next append overwrite a real
row. That is the sharpest edge in this change and it gets its own group.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from unittest import mock

from loguru import logger

from crawler.keywords import Keyword, load_keywords
from crawler.tech_filter import is_tech_job, why
from crawler.weekly_run import WeeklyRun
from sheets.client import (
    _RETRY_AFTER_CEILING,
    DestructiveRequestError,
    SheetsClient,
    _retry_after,
)
from sheets.companies import CompanyRepository as SheetCompanies
from sheets.init import seed_keywords
from sheets.jobs import JobRepository as SheetJobs
from sheets.runs import FailureRepository
from sheets.schema import CURRENT_JOBS, IT_KEYWORDS
from sheets.storage import FIRST_DATA_ROW, TabStore
from store import Database, migrate
from store.repositories import JobRepository as StoreJobs
from utils.http import MAX_RETRY_AFTER, BoundedRetry, build_session
from tests._fake_sheets import FakeSheetsService
from tests.test_weekly_run import FakeEngine, fixture, posting, result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def captured_logs(level: str = "DEBUG") -> Iterator[List[Tuple[str, str]]]:
    """Collect loguru records, which ``assertLogs`` cannot see.

    The project logs through loguru rather than the standard library, and
    loguru does not propagate to it, so a sink is the supported way in.

    Args:
        level: The lowest level to capture.

    Yields:
        ``(level_name, message)`` for everything logged in the block.
    """
    captured: List[Tuple[str, str]] = []

    def sink(message: Any) -> None:
        record = message.record
        captured.append((record["level"].name, record["message"]))

    sink_id = logger.add(sink, level=level, format="{message}")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


class _Response:
    """The one thing :meth:`urllib3.util.Retry.get_retry_after` reads."""

    def __init__(self, retry_after: Optional[str] = None) -> None:
        self.headers: Dict[str, str] = {}
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after


class _Error:
    """A googleapiclient error, as far as ``sheets.client`` is concerned."""

    def __init__(self, retry_after: Optional[str] = None) -> None:
        self.resp: Dict[str, str] = {"status": "429"}
        if retry_after is not None:
            self.resp["retry-after"] = retry_after


def sheet_row(name: str, website: str = "", career_url: str = "") -> Dict[str, str]:
    """A MASTER_COMPANIES row as an import produces it."""
    return {"company": name, "website": website, "career_url": career_url, "it_link": ""}


class HardeningRunTest(unittest.TestCase):
    """A fake spreadsheet, a real file database, and the JSON crawl path.

    The same shape as :mod:`tests.test_incremental_persistence`, because the
    behaviour under test only exists on the incremental path — it is exactly
    what upserting rather than replacing costs.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.checkpoint_path = self.root / "checkpoint.json"
        self.client, self.service = fixture()
        self.database = Database(self.root / "crawl.db")
        migrate(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    # -- fixtures ------------------------------------------------------------

    def seed(self, *rows: Mapping[str, str]) -> None:
        """Put company rows in the fake MASTER_COMPANIES tab."""
        SheetCompanies(self.client).import_rows([dict(row) for row in rows])

    def company(self, index: int) -> str:
        """Seed one company and return its key."""
        self.seed(
            sheet_row(f"Company {index}", f"c{index}.com", f"https://c{index}.com/careers")
        )
        return f"domain:c{index}.com"

    def runner(self, engine: Any, **kwargs: Any) -> WeeklyRun:
        """A JSON-path runner with a durable store behind it."""
        return WeeklyRun(
            self.client,
            engine=engine,
            checkpoint_path=self.checkpoint_path,
            database=self.database,
            batch_size=kwargs.pop("batch_size", 50),
            **kwargs,
        )

    def keywords_tab(self, *rows: Sequence[str]) -> None:
        """Write keyword rows into IT_KEYWORDS, as the operator would."""
        store = TabStore(self.client, IT_KEYWORDS)
        store.upsert(
            [
                {
                    "keyword": row[0],
                    "category": row[1] if len(row) > 1 else "",
                    "enabled": row[2] if len(row) > 2 else "TRUE",
                    "match_type": row[3] if len(row) > 3 else "phrase",
                }
                for row in rows
            ],
            key_field="keyword",
        )

    # -- readers -------------------------------------------------------------

    def current_rows(self) -> List[Dict[str, Any]]:
        """Every CURRENT_JOBS row, as records."""
        return [record.values for record in SheetJobs(self.client).current.read()]

    def current_keys(self) -> List[str]:
        """Every job key CURRENT_JOBS shows, duplicates included."""
        return SheetJobs(self.client).current_job_keys()

    def weekly_rows(self) -> List[Dict[str, Any]]:
        """Every NEW_LAST_WEEK row, as records."""
        return [record.values for record in SheetJobs(self.client).weekly.read()]

    def failure_keys(self) -> List[str]:
        """Every company key FAILURES shows."""
        return [
            record.get("company_key")
            for record in FailureRepository(self.client).store.read()
            if record.get("company_key")
        ]

    def stored_jobs(self) -> List[Dict[str, Any]]:
        """Every row in the SQLite jobs table."""
        return StoreJobs(self.database).all()


# ---------------------------------------------------------------------------
# 1. Retry-After is honoured, and bounded
# ---------------------------------------------------------------------------


class TestRetryAfterIsBounded(unittest.TestCase):
    """A wait the server asks for is respected up to a ceiling, never past it."""

    def setUp(self) -> None:
        self.policy = BoundedRetry(total=2, respect_retry_after_header=True)

    def test_a_short_wait_is_respected_exactly(self) -> None:
        """Under the ceiling, the server's number is the number."""
        self.assertEqual(self.policy.get_retry_after(_Response("30")), 30.0)

    def test_the_ceiling_itself_is_respected(self) -> None:
        """The boundary is inclusive, so 120 is honoured rather than clamped."""
        self.assertEqual(
            self.policy.get_retry_after(_Response(str(int(MAX_RETRY_AFTER)))),
            MAX_RETRY_AFTER,
        )

    def test_an_hour_is_capped(self) -> None:
        """The failure this exists for: Retry-After: 3600 parked a worker."""
        self.assertEqual(self.policy.get_retry_after(_Response("3600")), MAX_RETRY_AFTER)

    def test_six_hours_is_capped(self) -> None:
        """urllib3's own ceiling is 21,600 seconds, which is not a ceiling."""
        self.assertEqual(self.policy.get_retry_after(_Response("21600")), MAX_RETRY_AFTER)

    def test_no_header_falls_through_to_backoff(self) -> None:
        """``None`` is what makes urllib3 use its computed delay instead."""
        self.assertIsNone(self.policy.get_retry_after(_Response()))

    def test_a_negative_wait_is_not_an_instruction(self) -> None:
        """A malformed header must not become a hot loop, and must not raise.

        urllib3 raises ``InvalidHeader`` on a negative value rather than
        returning one, and uncaught that escapes ``Retry.sleep`` -- turning
        a server's malformed reply into a crawl failure.
        """
        self.assertIsNone(self.policy.get_retry_after(_Response("-30")))

    def test_an_unparseable_wait_is_ignored_rather_than_raised(self) -> None:
        self.assertIsNone(self.policy.get_retry_after(_Response("soon please")))
        self.assertIsNone(self.policy.get_retry_after(_Response("")))

    def test_a_date_in_the_past_is_safe(self) -> None:
        """HTTP-date form is legal, and one already elapsed clamps to zero."""
        seconds = self.policy.get_retry_after(
            _Response("Wed, 21 Oct 2015 07:28:00 GMT")
        )
        self.assertIsNotNone(seconds)
        self.assertGreaterEqual(seconds, 0.0)
        self.assertLessEqual(seconds, MAX_RETRY_AFTER)

    def test_a_far_future_date_is_capped(self) -> None:
        """The HTTP-date form has to be bounded as well as the seconds form."""
        seconds = self.policy.get_retry_after(
            _Response("Fri, 01 Jan 2100 00:00:00 GMT")
        )
        self.assertEqual(seconds, MAX_RETRY_AFTER)

    def test_the_ceiling_survives_a_copy(self) -> None:
        """The sharp edge.

        urllib3 rebuilds the policy through ``new()`` after every attempt, and
        copies only the fields it knows about. A ceiling lost on the copy would
        apply to the attempt that does not sleep and be gone for the one that
        does.
        """
        tightened = BoundedRetry(total=3, respect_retry_after_header=True)
        tightened.max_retry_after = 5.0

        copy = tightened.new()
        self.assertEqual(copy.max_retry_after, 5.0)
        self.assertEqual(copy.get_retry_after(_Response("3600")), 5.0)

        # And after the several copies a real request makes.
        for _ in range(3):
            copy = copy.increment(method="GET", url="https://example.test/")
        self.assertEqual(copy.get_retry_after(_Response("3600")), 5.0)

    def test_a_custom_ceiling_is_honoured(self) -> None:
        """Configurable, without being a knob nobody sets."""
        session = build_session(retries=2, retry_after_max=45.0)
        policy = session.get_adapter("https://example.test/").max_retries
        self.assertEqual(policy.get_retry_after(_Response("3600")), 45.0)


class TestRetryBehaviourIsOtherwiseUnchanged(unittest.TestCase):
    """Bounding one wait must not quietly disable retrying."""

    def setUp(self) -> None:
        self.session = build_session(retries=4)
        self.policy = self.session.get_adapter("https://example.test/").max_retries

    def test_the_session_uses_the_bounded_policy(self) -> None:
        self.assertIsInstance(self.policy, BoundedRetry)
        self.assertEqual(self.policy.max_retry_after, MAX_RETRY_AFTER)

    def test_retries_are_still_counted_from_the_attempt_budget(self) -> None:
        self.assertEqual(self.policy.total, 3)
        self.assertEqual(self.policy.connect, 3)
        self.assertEqual(self.policy.read, 3)
        self.assertEqual(self.policy.status, 3)

    def test_the_retryable_statuses_are_untouched(self) -> None:
        self.assertEqual(set(self.policy.status_forcelist), {429, 500, 502, 503, 504})

    def test_backoff_is_still_exponential(self) -> None:
        self.assertEqual(self.policy.backoff_factor, 1.0)

    def test_retry_after_is_still_respected_at_all(self) -> None:
        self.assertTrue(self.policy.respect_retry_after_header)

    def test_post_is_still_retried(self) -> None:
        """Several boards are POST-only, and always have been."""
        self.assertIn("POST", self.policy.allowed_methods)

    def test_a_status_is_not_raised_on(self) -> None:
        """The adapters classify statuses themselves; urllib3 must not raise."""
        self.assertFalse(self.policy.raise_on_status)

    def test_the_workday_session_is_bounded_too(self) -> None:
        """Workday hosts more of this roster than every other vendor combined."""
        from adapters.workday import build_session as workday_session

        policy = workday_session(3).get_adapter("https://x.myworkdayjobs.com/").max_retries
        self.assertIsInstance(policy, BoundedRetry)
        self.assertEqual(policy.get_retry_after(_Response("3600")), MAX_RETRY_AFTER)


class TestSheetsRetryAfterIsBounded(unittest.TestCase):
    """The same header, from Google, on the write side of the run."""

    def test_a_short_wait_is_respected(self) -> None:
        self.assertEqual(_retry_after(_Error("7")), 7.0)

    def test_a_quota_window_is_respected(self) -> None:
        """Sixty seconds is the window Sheets actually enforces."""
        self.assertEqual(_retry_after(_Error("60")), 60.0)

    def test_an_hour_is_capped(self) -> None:
        self.assertEqual(_retry_after(_Error("3600")), _RETRY_AFTER_CEILING)

    def test_a_negative_wait_is_clamped_to_zero(self) -> None:
        self.assertEqual(_retry_after(_Error("-1")), 0.0)

    def test_a_nonsense_wait_is_ignored(self) -> None:
        self.assertIsNone(_retry_after(_Error("soon")))

    def test_no_header_is_none(self) -> None:
        self.assertIsNone(_retry_after(_Error()))


# ---------------------------------------------------------------------------
# 2. Removing rows without corrupting the tab
# ---------------------------------------------------------------------------


class TestRemovalCompacts(unittest.TestCase):
    """A removal closes the gap behind it, because an append trusts the length."""

    def setUp(self) -> None:
        self.client, self.service = fixture()
        self.store = TabStore(self.client, CURRENT_JOBS)
        self.store.upsert(
            [{"job_key": f"j{index}", "job_title": f"Title {index}"} for index in range(5)],
            key_field="job_key",
        )

    def keys(self) -> List[str]:
        return [record.get("job_key") for record in self.store.read()]

    def test_a_middle_row_is_removed_and_the_rest_move_up(self) -> None:
        self.assertEqual(self.store.remove(["j2"], key_field="job_key"), 1)
        self.assertEqual(self.keys(), ["j0", "j1", "j3", "j4"])

    def test_the_survivors_are_contiguous_from_the_first_data_row(self) -> None:
        """The invariant the rest of the module rests on."""
        self.store.remove(["j0", "j2"], key_field="job_key")
        rows = [record.row for record in self.store.read()]
        self.assertEqual(rows, list(range(FIRST_DATA_ROW, FIRST_DATA_ROW + len(rows))))

    def test_an_append_after_a_removal_does_not_overwrite_a_survivor(self) -> None:
        """The bug a non-compacting removal would have caused.

        ``read`` skips blank rows and ``_write_rows`` appends at
        ``FIRST_DATA_ROW + len(read())``. Punch a blank into the middle and the
        next append lands on a row that still holds a posting.
        """
        self.store.remove(["j1"], key_field="job_key")
        self.store.upsert([{"job_key": "j9", "job_title": "Title 9"}], key_field="job_key")

        self.assertEqual(self.keys(), ["j0", "j2", "j3", "j4", "j9"])

    def test_removing_something_absent_writes_nothing(self) -> None:
        before = len(self.service.mutating_calls())
        self.assertEqual(self.store.remove(["nobody"], key_field="job_key"), 0)
        self.assertEqual(len(self.service.mutating_calls()), before)

    def test_removing_nothing_writes_nothing(self) -> None:
        before = len(self.service.mutating_calls())
        self.assertEqual(self.store.remove([], key_field="job_key"), 0)
        self.assertEqual(len(self.service.mutating_calls()), before)

    def test_a_second_removal_of_the_same_key_is_free(self) -> None:
        self.assertEqual(self.store.remove(["j2"], key_field="job_key"), 1)
        before = len(self.service.mutating_calls())
        self.assertEqual(self.store.remove(["j2"], key_field="job_key"), 0)
        self.assertEqual(len(self.service.mutating_calls()), before)

    def test_every_copy_of_a_duplicated_key_goes(self) -> None:
        """A tab holding a posting twice must not keep the second copy."""
        self.store.append([{"job_key": "j2", "job_title": "Title 2 again"}])
        self.assertEqual(self.keys().count("j2"), 2)

        self.assertEqual(self.store.remove(["j2"], key_field="job_key"), 2)
        self.assertNotIn("j2", self.keys())

    def test_a_row_with_no_key_is_left_alone(self) -> None:
        """An operator's own note is not the crawler's to tidy away."""
        self.store.append([{"job_title": "an operator note"}])
        self.store.remove(["j0"], key_field="job_key")

        titles = [record.get("job_title") for record in self.store.read()]
        self.assertIn("an operator note", titles)

    def test_a_dry_run_counts_without_writing(self) -> None:
        before = len(self.service.mutating_calls())
        self.assertEqual(self.store.remove(["j1"], key_field="job_key", dry_run=True), 1)
        self.assertEqual(len(self.service.mutating_calls()), before)
        self.assertIn("j1", self.keys())

    def test_nothing_destructive_is_sent(self) -> None:
        """Rows are blanked and rewritten, never deleted at the API level."""
        self.store.remove(["j0", "j1", "j2"], key_field="job_key")
        self.assertEqual(self.service.destructive_requests(), [])

    def test_the_grid_never_grows(self) -> None:
        rows_before = self.service.grid[CURRENT_JOBS.title][0]
        self.store.remove(["j0", "j1"], key_field="job_key")
        self.assertLessEqual(self.service.grid[CURRENT_JOBS.title][0], rows_before)


# ---------------------------------------------------------------------------
# 3. CURRENT_JOBS holds what is open now
# ---------------------------------------------------------------------------


class TestCurrentJobsReconciliation(HardeningRunTest):
    """A posting the ledger has closed does not stay in the snapshot."""

    def open_then_close(self) -> str:
        """Crawl one company with a posting, then again with none.

        Returns:
            The company key.
        """
        key = self.company(1)

        self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-1")

        self.runner(FakeEngine({key: result(jobs=[])})).execute(run_id="run-2")
        return key

    def test_a_posting_that_closes_leaves_the_snapshot(self) -> None:
        self.open_then_close()
        self.assertEqual(self.current_keys(), [])

    def test_the_ledger_keeps_it(self) -> None:
        """Removed from a report, never from the record."""
        self.open_then_close()
        stored = self.stored_jobs()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["status"], "closed")
        self.assertTrue(stored[0]["closed_at"])

    def test_the_weekly_log_keeps_the_closure(self) -> None:
        """NEW_LAST_WEEK is the history of what changed, and it is untouched."""
        self.open_then_close()
        changes = {row.get("change") for row in self.weekly_rows()}
        self.assertIn("closed", changes)

    def test_an_open_posting_survives(self) -> None:
        """Reconciliation must remove only what the ledger says is closed."""
        key = self.company(1)
        jobs = [
            posting(title="Software Engineer", url="https://c1.com/jobs/1"),
            posting(title="Data Engineer", url="https://c1.com/jobs/2"),
        ]
        self.runner(FakeEngine({key: result(jobs=jobs)})).execute(run_id="run-1")
        self.assertEqual(len(self.current_keys()), 2)

        self.runner(FakeEngine({key: result(jobs=jobs[:1])})).execute(run_id="run-2")

        remaining = self.current_rows()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["job_title"], "Software Engineer")

    def test_a_posting_closed_by_an_earlier_run_is_swept_up_later(self) -> None:
        """The backlog case.

        A run that closed a posting and then died never reconciled. The next
        run must clear it, because the pass is driven by the ledger's current
        state rather than by what this run happened to observe.
        """
        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-1")

        stale = self.current_keys()[0]

        # Close it in the ledger without letting the run tidy up after itself.
        StoreJobs(self.database).close_missing(key, [], run_id="run-2")
        self.assertEqual(self.current_keys(), [stale])

        second = self.company(2)
        self.runner(FakeEngine({second: result(jobs=[])})).execute(run_id="run-3")

        self.assertNotIn(stale, self.current_keys())

    def test_a_duplicate_row_for_a_closed_posting_goes_entirely(self) -> None:
        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-1")

        duplicated = self.current_keys()[0]
        SheetJobs(self.client).current.append([{"job_key": duplicated}])
        self.assertEqual(self.current_keys().count(duplicated), 2)

        self.runner(FakeEngine({key: result(jobs=[])})).execute(run_id="run-2")
        self.assertEqual(self.current_keys(), [])

    def test_a_posting_the_ledger_never_saw_is_left_alone(self) -> None:
        """Storage cannot testify that something it never saw has ended."""
        SheetJobs(self.client).current.upsert(
            [{"job_key": "hand-typed", "job_title": "Added by the operator"}],
            key_field="job_key",
        )
        key = self.company(1)
        self.runner(FakeEngine({key: result(jobs=[])})).execute(run_id="run-1")

        self.assertIn("hand-typed", self.current_keys())

    def test_running_the_same_run_again_removes_nothing_more(self) -> None:
        """Idempotent: the second pass finds nothing and writes nothing."""
        key = self.open_then_close()

        before = len(self.service.mutating_calls())
        run = self.runner(FakeEngine({key: result(jobs=[])}))
        summary = run.execute(run_id="run-3")

        self.assertEqual(summary.current_jobs_removed, 0)
        # Writes still happen for the run's own rows; none of them are removals.
        self.assertEqual(self.current_keys(), [])
        self.assertGreaterEqual(len(self.service.mutating_calls()), before)

    def test_the_removal_is_counted_on_the_summary(self) -> None:
        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-1")

        summary = self.runner(FakeEngine({key: result(jobs=[])})).execute(run_id="run-2")
        self.assertEqual(summary.current_jobs_removed, 1)

    def test_a_dry_run_removes_nothing(self) -> None:
        """A dry run authenticates read-only; it must not try to tidy."""
        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-1")
        shown = self.current_keys()

        summary = self.runner(FakeEngine({key: result(jobs=[])})).execute(
            run_id="run-2", dry_run=True
        )
        self.assertEqual(summary.current_jobs_removed, 0)
        self.assertEqual(self.current_keys(), shown)

    def test_nothing_destructive_is_sent(self) -> None:
        self.open_then_close()
        self.assertEqual(self.service.destructive_requests(), [])


# ---------------------------------------------------------------------------
# 4. FAILURES holds what is unresolved now
# ---------------------------------------------------------------------------


class TestFailuresReconciliation(HardeningRunTest):
    """A company that has since been read stops accusing itself."""

    def test_a_failing_company_is_reported(self) -> None:
        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[], error="HTTP 403: forbidden")})
        ).execute(run_id="run-1")

        self.assertEqual(self.failure_keys(), [key])

    def test_succeeding_later_clears_the_stale_row(self) -> None:
        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[], error="HTTP 403: forbidden")})
        ).execute(run_id="run-1")
        self.assertEqual(self.failure_keys(), [key])

        self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-2")

        self.assertEqual(self.failure_keys(), [])

    def test_an_empty_board_counts_as_read(self) -> None:
        """A board with no openings was read; its failure is resolved."""
        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[], error="HTTP 429: slow down")})
        ).execute(run_id="run-1")

        self.runner(FakeEngine({key: result(jobs=[])})).execute(run_id="run-2")
        self.assertEqual(self.failure_keys(), [])

    def test_a_company_still_failing_keeps_its_row(self) -> None:
        first = self.company(1)
        second = self.company(2)

        engine = FakeEngine(
            {
                first: result(jobs=[], error="HTTP 403: forbidden"),
                second: result(jobs=[], error="HTTP 500: broken"),
            }
        )
        self.runner(engine).execute(run_id="run-1")
        self.assertEqual(sorted(self.failure_keys()), sorted([first, second]))

        self.runner(
            FakeEngine(
                {
                    first: result(jobs=[posting(url="https://c1.com/jobs/1")]),
                    second: result(jobs=[], error="HTTP 500: still broken"),
                }
            )
        ).execute(run_id="run-2")

        self.assertEqual(self.failure_keys(), [second])

    def test_a_company_not_attempted_keeps_its_row(self) -> None:
        """Not attempted is not resolved."""
        first = self.company(1)
        second = self.company(2)

        self.runner(
            FakeEngine(
                {
                    first: result(jobs=[], error="HTTP 403: forbidden"),
                    second: result(jobs=[], error="HTTP 403: forbidden"),
                }
            )
        ).execute(run_id="run-1")
        self.assertEqual(len(self.failure_keys()), 2)

        # Only the first company is crawled this time.
        self.runner(
            FakeEngine({first: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-2", limit=1)

        self.assertEqual(self.failure_keys(), [second])

    def test_an_unusable_row_survives(self) -> None:
        """A row naming no URL was never crawled, so it is never resolved."""
        self.seed(sheet_row("No URL At All"))
        key = self.company(1)

        self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-1")

        reported = self.failure_keys()
        self.assertEqual(len(reported), 1)
        self.assertNotEqual(reported[0], key)

    def test_one_company_failing_twice_in_a_run_occupies_one_row(self) -> None:
        """Keyed on the company, so a retry cannot double it."""
        key = self.company(1)
        run = self.runner(
            FakeEngine({key: result(jobs=[], error="HTTP 403: forbidden")}),
            batch_size=1,
        )
        run.execute(run_id="run-1")
        run2 = self.runner(
            FakeEngine({key: result(jobs=[], error="HTTP 403: forbidden")}),
            batch_size=1,
        )
        run2.execute(run_id="run-2")

        self.assertEqual(self.failure_keys(), [key])

    def test_a_replay_of_a_successful_run_still_clears(self) -> None:
        """Resolution is driven by the checkpoint, so a replay converges."""
        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[], error="HTTP 403: forbidden")})
        ).execute(run_id="run-1")

        engine = FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        self.runner(engine).execute(run_id="run-2")
        self.runner(engine).execute(run_id="run-2")

        self.assertEqual(self.failure_keys(), [])

    def test_the_resolution_is_counted_on_the_summary(self) -> None:
        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[], error="HTTP 403: forbidden")})
        ).execute(run_id="run-1")

        summary = self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-2")

        self.assertEqual(summary.failures_resolved, 1)

    def test_the_weekly_run_row_still_records_the_failure(self) -> None:
        """History lives in WEEKLY_RUNS; the tab is only the current state."""
        from sheets.runs import RunRepository

        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[], error="HTTP 403: forbidden")})
        ).execute(run_id="run-1")
        self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-2")

        rows = {
            record.get("run_id"): record.values
            for record in RunRepository(self.client).store.read()
        }
        self.assertEqual(rows["run-1"]["companies_failed"], "1")

    def test_a_dry_run_resolves_nothing(self) -> None:
        key = self.company(1)
        self.runner(
            FakeEngine({key: result(jobs=[], error="HTTP 403: forbidden")})
        ).execute(run_id="run-1")

        summary = self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-2", dry_run=True)

        self.assertEqual(summary.failures_resolved, 0)
        self.assertEqual(self.failure_keys(), [key])


# ---------------------------------------------------------------------------
# 4b. The run-level failure breakdown
# ---------------------------------------------------------------------------


class TestFailureBreakdownIsComplete(HardeningRunTest):
    """What the dashboard says failed must be what actually failed.

    The defect: ``_persist_segment`` accumulates each batch's failures onto the
    summary and then clears the list it was handed, so by the end of the run
    that list holds only the unusable rows appended after the loop. Assigning
    it -- rather than appending to it -- threw away every real failure, and the
    dashboard then reported the handful of rows naming no URL as the whole
    story. The FAILURES tab was always right; only the summary was wrong.
    """

    def failing(self, *keys: str, error: str = "HTTP 403: forbidden") -> Any:
        """An engine that fails every named company."""
        return FakeEngine({key: result(jobs=[], error=error) for key in keys})

    def test_failures_from_every_batch_reach_the_summary(self) -> None:
        """Three batches, six failures, one complete breakdown."""
        keys = [self.company(index) for index in range(1, 7)]

        summary = self.runner(self.failing(*keys), batch_size=2).execute(run_id="run-1")

        self.assertEqual(summary.companies_failed, 6)
        self.assertEqual(len(summary.failures), 6)
        self.assertEqual(sum(summary.blockers.values()), 6)

    def test_a_later_batch_does_not_erase_an_earlier_one(self) -> None:
        """The exact shape of the bug: batch one's failures must survive."""
        first = self.company(1)
        second = self.company(2)

        summary = self.runner(
            FakeEngine(
                {
                    first: result(jobs=[], error="AdapterHttpError: HTTP 403: forbidden"),
                    second: result(jobs=[], error="AdapterHttpError: HTTP 429: slow down"),
                }
            ),
            batch_size=1,
        ).execute(run_id="run-1")

        self.assertEqual(len(summary.failures), 2)
        self.assertEqual(sum(summary.blockers.values()), 2)
        self.assertGreaterEqual(len(summary.blockers), 2)

    def test_an_unusable_row_is_added_not_substituted(self) -> None:
        """Unusable rows are appended after the loop; they must not replace."""
        key = self.company(1)
        self.seed(sheet_row("No URL At All"))

        summary = self.runner(self.failing(key), batch_size=1).execute(run_id="run-1")

        self.assertEqual(sum(summary.blockers.values()), 2)
        keys = {record.get("company_key") for record in summary.failures}
        self.assertIn(key, keys)
        self.assertEqual(len(keys), 2)

    def test_the_breakdown_matches_the_failures_tab(self) -> None:
        """The tab has always been right; the summary now agrees with it."""
        keys = [self.company(index) for index in range(1, 5)]
        summary = self.runner(self.failing(*keys), batch_size=2).execute(run_id="run-1")

        self.assertEqual(
            summary.blockers, FailureRepository(self.client).counts_by_type()
        )

    def test_the_breakdown_spans_resume_segments(self) -> None:
        """A resumed run reports the week's failures, not just this process's.

        The first segment fails two companies and is interrupted. The second
        segment does not re-crawl them -- they are checkpointed -- so it never
        observes their failures, yet they are unresolved and belong in the
        week's breakdown.
        """
        keys = [self.company(index) for index in range(1, 5)]

        first = self.runner(
            FakeEngine(
                {key: result(jobs=[], error="HTTP 403: forbidden") for key in keys},
                fail_after=2,
            ),
            batch_size=2,
        )
        with self.assertRaises(KeyboardInterrupt):
            first.execute(run_id="run-1")

        self.assertEqual(len(self.failure_keys()), 2)

        second = self.runner(
            FakeEngine({key: result(jobs=[], error="HTTP 500: broken") for key in keys}),
            batch_size=2,
        )
        summary = second.execute(run_id="run-1")

        # Two from the interrupted segment, two from this one: four unresolved.
        self.assertEqual(len(self.failure_keys()), 4)
        self.assertEqual(sum(summary.blockers.values()), 4)

    def test_a_resolved_company_leaves_the_breakdown(self) -> None:
        """The breakdown counts what is unresolved, not what has ever failed."""
        first = self.company(1)
        second = self.company(2)

        self.runner(self.failing(first, second)).execute(run_id="run-1")

        summary = self.runner(
            FakeEngine(
                {
                    first: result(jobs=[posting(url="https://c1.com/jobs/1")]),
                    second: result(jobs=[], error="HTTP 500: broken"),
                }
            )
        ).execute(run_id="run-2")

        self.assertEqual(sum(summary.blockers.values()), 1)
        self.assertEqual(self.failure_keys(), [second])

    def test_a_clean_run_reports_no_blockers(self) -> None:
        key = self.company(1)
        summary = self.runner(
            FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        ).execute(run_id="run-1")

        self.assertEqual(summary.blockers, {})
        self.assertEqual(summary.failures, [])

    def test_the_dashboard_shows_the_full_breakdown(self) -> None:
        """End to end: the section the operator actually reads."""
        keys = [self.company(index) for index in range(1, 5)]
        summary = self.runner(self.failing(*keys), batch_size=2).execute(run_id="run-1")

        sections = dict(summary.dashboard_sections())
        self.assertIn("Failures", sections)
        self.assertEqual(sum(count for _label, count in sections["Failures"]), 4)

    def test_a_dry_run_still_reports_its_own_failures(self) -> None:
        """Nothing is written, so the count comes from what was observed."""
        keys = [self.company(index) for index in range(1, 4)]
        summary = self.runner(self.failing(*keys), batch_size=1).execute(
            run_id="run-1", dry_run=True
        )

        self.assertEqual(summary.companies_failed, 3)
        self.assertEqual(sum(summary.blockers.values()), 3)

    def test_the_crawl_itself_is_unchanged(self) -> None:
        """A reporting fix must not alter what was crawled or stored."""
        key = self.company(1)
        engine = FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
        summary = self.runner(engine).execute(run_id="run-1")

        self.assertEqual(engine.crawled, [key])
        self.assertEqual(summary.observations, 1)
        self.assertEqual(summary.companies_succeeded, 1)
        self.assertEqual(len(self.stored_jobs()), 1)


# ---------------------------------------------------------------------------
# 5. IT_KEYWORDS drives production
# ---------------------------------------------------------------------------


class TestKeywordMatching(unittest.TestCase):
    """The classifier honours what the tab says, not merely that it exists."""

    def test_a_term_the_built_in_tables_do_not_know_is_not_technical(self) -> None:
        """The baseline the rest of this group rests on."""
        self.assertFalse(is_tech_job("Widgetron Coordinator"))
        self.assertFalse(is_tech_job("Gadgetron Coordinator"))

    def test_an_enabled_term_makes_it_technical(self) -> None:
        keyword = Keyword("Widgetron", category="ERP Platforms")
        self.assertTrue(is_tech_job("Widgetron Coordinator", extra_keywords=[keyword]))

    def test_the_category_is_reported_with_the_verdict(self) -> None:
        keyword = Keyword("Widgetron", category="ERP Platforms")
        verdict, reason = why("Widgetron Coordinator", extra_keywords=[keyword])
        self.assertTrue(verdict)
        self.assertIn("ERP Platforms", reason)

    def test_phrase_matching_does_not_fire_inside_a_longer_word(self) -> None:
        """The default, and the reason SAP does not match 'sapphire'."""
        keyword = Keyword("SAP", category="ERP Platforms")
        self.assertFalse(is_tech_job("Sapphire Polisher", extra_keywords=[keyword]))

    def test_substring_matching_does_when_asked_for(self) -> None:
        keyword = Keyword("SAP", category="ERP Platforms", match_type="substring")
        self.assertTrue(is_tech_job("Sapphire Polisher", extra_keywords=[keyword]))

    def test_a_bare_string_still_behaves_as_it_always_did(self) -> None:
        """Every existing caller passes strings, and they keep phrase matching."""
        self.assertTrue(is_tech_job("Widgetron Coordinator", extra_keywords=["widgetron"]))
        self.assertFalse(is_tech_job("Sapphire Polisher", extra_keywords=["sap"]))

    def test_an_exclusion_still_beats_a_configured_term(self) -> None:
        """A keyword list cannot drag a sales role into the technology tab."""
        keyword = Keyword("Engineer", category="Anything")
        self.assertFalse(is_tech_job("Sales Engineer", extra_keywords=[keyword]))


class TestKeywordLoading(unittest.TestCase):
    """Reading the tab, and saying so when it cannot be read."""

    def setUp(self) -> None:
        self.client, self.service = fixture()

    def test_a_disabled_term_is_not_loaded(self) -> None:
        TabStore(self.client, IT_KEYWORDS).upsert(
            [
                {"keyword": "Widgetron", "category": "ERP", "enabled": "TRUE"},
                {"keyword": "Gadgetron", "category": "ERP", "enabled": "FALSE"},
            ],
            key_field="keyword",
        )
        loaded = {keyword.keyword for keyword in load_keywords(self.client)}
        self.assertEqual(loaded, {"Widgetron"})

    def test_the_seeded_erp_list_loads_in_full(self) -> None:
        """The seventy terms the operator's sheet was created with."""
        from sheets.init import DEFAULT_IT_KEYWORDS

        seed_keywords(self.client, IT_KEYWORDS.title)
        loaded = load_keywords(self.client)

        self.assertEqual(len(loaded), len(DEFAULT_IT_KEYWORDS))
        self.assertIn("ERP Platforms", loaded.categories())
        self.assertEqual(loaded.category_of("peoplesoft"), "ERP Platforms")

    def test_an_unreadable_tab_is_reported_at_error(self) -> None:
        """Clearly, not silently. This used to be a debug line."""
        with mock.patch(
            "sheets.storage.TabStore.read", side_effect=RuntimeError("no such tab")
        ):
            with captured_logs("ERROR") as records:
                loaded = load_keywords(self.client)

        self.assertEqual(len(loaded), 0)
        self.assertTrue(
            any(level == "ERROR" and "IT_KEYWORDS" in message
                for level, message in records),
            f"no ERROR naming IT_KEYWORDS was logged: {records}",
        )

    def test_an_empty_tab_is_reported_at_warning(self) -> None:
        with captured_logs("WARNING") as records:
            loaded = load_keywords(self.client)

        self.assertEqual(len(loaded), 0)
        self.assertTrue(
            any(level == "WARNING" and "IT_KEYWORDS" in message
                for level, message in records),
            f"no WARNING naming IT_KEYWORDS was logged: {records}",
        )


class TestKeywordsReachProduction(HardeningRunTest):
    """The weekly path loads the tab, uses it, and does so once."""

    def keyword_reads(self) -> int:
        """How many times IT_KEYWORDS was read from the API."""
        return sum(
            1
            for kind, payload in self.service.calls
            if kind == "values.get" and "IT_KEYWORDS" in str(payload.get("range", ""))
        )

    def crawl(self, *titles: str, run_id: str = "run-1") -> Any:
        """Crawl one company advertising these titles."""
        key = self.company(1)
        jobs = [
            posting(title=title, url=f"https://c1.com/jobs/{index}")
            for index, title in enumerate(titles)
        ]
        return self.runner(FakeEngine({key: result(jobs=jobs)})).execute(run_id=run_id)

    def test_an_enabled_keyword_classifies_a_posting_as_technical(self) -> None:
        self.keywords_tab(("Widgetron", "ERP Platforms", "TRUE", "phrase"))
        self.crawl("Widgetron Coordinator")

        stored = self.stored_jobs()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["is_tech"], 1)
        self.assertEqual(len(self.current_keys()), 1)

    def test_a_disabled_keyword_does_not(self) -> None:
        self.keywords_tab(("Gadgetron", "ERP Platforms", "FALSE", "phrase"))
        self.crawl("Gadgetron Coordinator")

        stored = self.stored_jobs()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["is_tech"], 0)
        self.assertEqual(self.current_keys(), [])

    def test_match_type_reaches_the_classifier(self) -> None:
        self.keywords_tab(("SAP", "ERP Platforms", "TRUE", "substring"))
        self.crawl("Sapphire Polisher")

        self.assertEqual(self.stored_jobs()[0]["is_tech"], 1)

    def test_the_run_records_which_list_was_in_effect(self) -> None:
        """So a verdict can be traced back to the list that produced it."""
        self.keywords_tab(
            ("Widgetron", "ERP Platforms", "TRUE", "phrase"),
            ("Sprocketflow", "Middleware", "TRUE", "phrase"),
        )
        summary = self.crawl("Widgetron Coordinator")

        self.assertEqual(summary.keywords_active, 2)
        self.assertEqual(summary.keyword_categories, 2)
        self.assertTrue(summary.keyword_fingerprint)

    def test_the_tab_is_read_once_for_the_whole_run(self) -> None:
        """Not once per company, and certainly not once per posting."""
        self.keywords_tab(("Widgetron", "ERP Platforms", "TRUE", "phrase"))

        for index in range(2, 8):
            self.company(index)

        before = self.keyword_reads()
        run = self.runner(
            FakeEngine(
                {
                    f"domain:c{index}.com": result(
                        jobs=[
                            posting(title="Widgetron Coordinator", url=f"https://c{index}.com/j/1"),
                            posting(title="Widgetron Analyst", url=f"https://c{index}.com/j/2"),
                        ]
                    )
                    for index in range(2, 8)
                }
            ),
            batch_size=2,
        )
        calls = []
        real = load_keywords

        def spy(client, **kwargs):
            """Count the loads without changing what they return."""
            calls.append(client)
            return real(client, **kwargs)

        with mock.patch("crawler.weekly_run.load_keywords", spy):
            summary = run.execute(run_id="run-1")

        self.assertEqual(summary.companies_attempted, 6)
        self.assertEqual(summary.observations, 12)
        self.assertEqual(
            len(calls),
            1,
            "IT_KEYWORDS was loaded more than once for a run of three batches",
        )

        # And the API cost is a constant rather than a function of how much was
        # crawled: one header read plus one data read, whatever the roster size.
        self.assertLessEqual(
            self.keyword_reads() - before,
            2,
            "a per-batch or per-company IT_KEYWORDS call crept in",
        )

    def test_the_keyword_cost_does_not_grow_with_the_roster(self) -> None:
        """One company or six, the tab costs exactly the same."""
        self.keywords_tab(("Widgetron", "ERP Platforms", "TRUE", "phrase"))

        before = self.keyword_reads()
        self.crawl("Widgetron Coordinator", run_id="run-small")
        small = self.keyword_reads() - before

        for index in range(2, 8):
            self.company(index)

        before = self.keyword_reads()
        self.runner(
            FakeEngine(
                {
                    f"domain:c{index}.com": result(
                        jobs=[
                            posting(
                                title="Widgetron Coordinator",
                                url=f"https://c{index}.com/j/1",
                            )
                        ]
                    )
                    for index in range(2, 8)
                }
            ),
            batch_size=2,
        ).execute(run_id="run-large")
        large = self.keyword_reads() - before

        self.assertEqual(small, large)

    def test_the_keyword_set_is_actually_handed_to_the_classifier(self) -> None:
        """Observed at the boundary, rather than inferred from the outcome."""
        self.keywords_tab(("Widgetron", "ERP Platforms", "TRUE", "phrase"))

        seen: List[Any] = []
        real = __import__("crawler.observations", fromlist=["is_tech_job"]).is_tech_job

        def spy(title: str, department: str = "", extra_keywords: Any = ()) -> bool:
            seen.append(list(extra_keywords))
            return real(title, department, extra_keywords)

        with mock.patch("crawler.observations.is_tech_job", spy):
            self.crawl("Widgetron Coordinator")

        self.assertTrue(seen, "the classifier was never called")
        terms = [getattr(item, "keyword", item) for item in seen[0]]
        self.assertIn("Widgetron", terms)

    def test_an_unreadable_tab_does_not_stop_the_run(self) -> None:
        """Losing the operator's terms is bad; losing the crawl would be worse."""
        with mock.patch(
            "crawler.weekly_run.load_keywords", side_effect=RuntimeError("tab is gone")
        ):
            summary = self.crawl("Software Engineer")

        self.assertEqual(summary.companies_succeeded, 1)
        self.assertEqual(summary.keywords_active, 0)
        # The built-in tables still classify, so nothing silently stops working.
        self.assertEqual(self.stored_jobs()[0]["is_tech"], 1)

    def test_a_caller_supplied_keyword_is_kept_alongside_the_sheet(self) -> None:
        self.keywords_tab(("Widgetron", "ERP Platforms", "TRUE", "phrase"))
        key = self.company(1)

        run = self.runner(
            FakeEngine(
                {
                    key: result(
                        jobs=[
                            posting(title="Widgetron Coordinator", url="https://c1.com/j/1"),
                            posting(title="Sprocketflow Coordinator", url="https://c1.com/j/2"),
                        ]
                    )
                }
            )
        )
        run.execute(run_id="run-1", extra_keywords=["sprocketflow"])

        self.assertEqual({row["is_tech"] for row in self.stored_jobs()}, {1})


# ---------------------------------------------------------------------------
# 6. The frozen JOB_HISTORY baseline announces itself
# ---------------------------------------------------------------------------


class TestStaleBaselineIsAnnounced(HardeningRunTest):
    """A comparison against the frozen tab must not pass for a real one.

    ``JOB_HISTORY`` stopped being written when SQLite became the ledger. Any
    run without a durable store still falls back to it -- and ``main()`` opens
    no database for a ``--dry-run``, so that is every dry run. The baseline is
    then a tab frozen on the day the switch happened, and everything stored
    since is reported as new.

    Not fixed here: choosing a baseline is ``apply``'s contract and changing it
    belongs with the caller that owns the store. But it is announced, because a
    wrong number nobody can identify as wrong is worse than no number.
    """

    def test_the_fallback_says_so(self) -> None:
        key = self.company(1)
        run = WeeklyRun(
            self.client,
            engine=FakeEngine(
                {key: result(jobs=[posting(url="https://c1.com/jobs/1")])}
            ),
            checkpoint_path=self.checkpoint_path,
            database=None,
        )

        with captured_logs("WARNING") as records:
            run.execute(run_id="run-1", dry_run=True)

        self.assertTrue(
            any(
                level == "WARNING"
                and "JOB_HISTORY" in message
                and "crawler.status" in message
                for level, message in records
            ),
            f"the stale-baseline warning was not logged: {records}",
        )

    def test_a_run_with_a_store_does_not_warn(self) -> None:
        """The incremental path reads SQLite, so there is nothing to warn about."""
        key = self.company(1)

        with captured_logs("WARNING") as records:
            self.runner(
                FakeEngine({key: result(jobs=[posting(url="https://c1.com/jobs/1")])})
            ).execute(run_id="run-1")

        self.assertFalse(
            [m for level, m in records if "JOB_HISTORY" in m and "crawler.status" in m],
            "warned about a stale baseline on a run that used the ledger",
        )

    def test_the_warning_changes_no_counts(self) -> None:
        """Purely informational: the comparison itself is untouched."""
        key = self.company(1)
        run = WeeklyRun(
            self.client,
            engine=FakeEngine(
                {key: result(jobs=[posting(url="https://c1.com/jobs/1")])}
            ),
            checkpoint_path=self.checkpoint_path,
            database=None,
        )
        summary = run.execute(run_id="run-1", dry_run=True)

        self.assertEqual(summary.observations, 1)
        self.assertEqual(summary.companies_succeeded, 1)
        self.assertIsNotNone(summary.changes)
        self.assertEqual(len(summary.changes.new_jobs), 1)


# ---------------------------------------------------------------------------
# 7. A grid is never made smaller
# ---------------------------------------------------------------------------


class TestEnsureSizeOnlyGrows(unittest.TestCase):
    """``ensure_size`` promised to grow only, and did not.

    It sent whatever it was handed. ``sheets.init`` hands it the size the
    *data* needs -- ``max(DEFAULT_TAB_ROWS, rows + 2)`` -- so initialising a tab
    whose grid was larger than its contents reduced that grid, and Google
    removed everything past the new boundary. On the production spreadsheet
    that took MASTER_COMPANIES from 25,458 rows to 16,082; it cost nothing only
    because the 9,376 rows it discarded were empty.

    The reason it survived a suite of 1,500 tests is in
    :mod:`tests._fake_sheets`: the simulator clamped a shrinking request upwards,
    so it was the *fake* honouring the promise the client had broken. That clamp
    is gone, which is what lets these tests mean anything.
    """

    def setUp(self) -> None:
        self.service = FakeSheetsService({"T": []})
        self.client = SheetsClient(self.service, "fake", sleep=lambda _s: None)
        self.sheet_id = self.service.sheet_ids["T"]

    def structural(self) -> int:
        """How many structural requests have been sent."""
        return len(self.service.structural_kinds)

    # -- growing, which must still work --------------------------------------

    def test_a_larger_request_expands(self) -> None:
        self.client.ensure_size(self.sheet_id, 5000, 40)
        self.assertEqual(self.service.grid["T"], (5000, 40))

    def test_growth_from_the_default_grid_works(self) -> None:
        """The ordinary case: a tab outgrowing the 1000x26 it was made with."""
        self.assertEqual(self.service.grid["T"], (1000, 26))
        self.client.ensure_size(self.sheet_id, 1200, 26)
        self.assertEqual(self.service.grid["T"], (1200, 26))

    def test_one_dimension_may_grow_while_the_other_is_kept(self) -> None:
        """Asking for more rows and fewer columns grows rows, keeps columns."""
        self.client.ensure_size(self.sheet_id, 5000, 40)
        self.client.ensure_size(self.sheet_id, 9000, 4)
        self.assertEqual(self.service.grid["T"], (9000, 40))

    # -- shrinking, which must not ------------------------------------------

    def test_a_smaller_request_leaves_the_grid_alone(self) -> None:
        """The bug, in one assertion."""
        self.client.ensure_size(self.sheet_id, 5000, 40)
        self.client.ensure_size(self.sheet_id, 10, 4)
        self.assertEqual(self.service.grid["T"], (5000, 40))

    def test_the_production_shape_is_reproduced(self) -> None:
        """MASTER_COMPANIES: a 25,458-row grid holding 16,080 rows of data."""
        self.service.grid["T"] = (25_458, 24)
        self.client.invalidate()

        self.client.ensure_size(self.sheet_id, max(1000, 16_080 + 2), 24)

        self.assertEqual(self.service.grid["T"], (25_458, 24))

    def test_a_smaller_request_sends_nothing_at_all(self) -> None:
        """Not merely clamped -- not sent. A no-op costs no structural quota."""
        self.client.ensure_size(self.sheet_id, 5000, 40)
        before = self.structural()

        self.client.ensure_size(self.sheet_id, 10, 4)

        self.assertEqual(self.structural(), before)

    def test_an_equal_request_is_a_no_op(self) -> None:
        self.client.ensure_size(self.sheet_id, 5000, 40)
        before = self.structural()

        self.client.ensure_size(self.sheet_id, 5000, 40)

        self.assertEqual(self.structural(), before)
        self.assertEqual(self.service.grid["T"], (5000, 40))

    def test_no_row_of_data_is_lost(self) -> None:
        """The consequence the grid size actually has."""
        self.client.write("'T'!A1", [[f"row {index}"] for index in range(1, 51)])
        self.client.ensure_size(self.sheet_id, 5000, 40)

        self.client.ensure_size(self.sheet_id, 3, 1)

        rows = self.client.read("'T'!A1:A50")
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0][0], "row 1")
        self.assertEqual(rows[-1][0], "row 50")

    def test_nothing_destructive_is_ever_sent(self) -> None:
        self.client.ensure_size(self.sheet_id, 5000, 40)
        self.client.ensure_size(self.sheet_id, 10, 4)
        self.assertEqual(self.service.destructive_requests(), [])


class TestTheGuardRefusesAShrink(unittest.TestCase):
    """The second layer: a shrink is refused however it is constructed.

    Clamping inside ``ensure_size`` fixes today's caller. The guard is for
    tomorrow's -- ``updateSheetProperties`` is a legitimate request that becomes
    destructive at a particular value, which is not something a list of banned
    request *kinds* can express.
    """

    def setUp(self) -> None:
        self.service = FakeSheetsService({"T": []})
        self.client = SheetsClient(self.service, "fake", sleep=lambda _s: None)
        self.sheet_id = self.service.sheet_ids["T"]
        self.client.ensure_size(self.sheet_id, 5000, 40)

    def request(self, **grid: int) -> Dict[str, Any]:
        """A hand-rolled resize request, as a future caller might build one."""
        return {
            "updateSheetProperties": {
                "properties": {"sheetId": self.sheet_id, "gridProperties": dict(grid)},
                "fields": "gridProperties.rowCount,gridProperties.columnCount",
            }
        }

    def test_shrinking_rows_is_refused(self) -> None:
        with self.assertRaises(DestructiveRequestError) as caught:
            self.client.batch_update([self.request(rowCount=10, columnCount=40)])
        self.assertIn("rowCount 5000 -> 10", str(caught.exception))

    def test_shrinking_columns_is_refused(self) -> None:
        with self.assertRaises(DestructiveRequestError) as caught:
            self.client.batch_update([self.request(rowCount=5000, columnCount=4)])
        self.assertIn("columnCount 40 -> 4", str(caught.exception))

    def test_the_refusal_happens_before_anything_is_sent(self) -> None:
        before = len(self.service.structural_kinds)
        with self.assertRaises(DestructiveRequestError):
            self.client.batch_update(
                [
                    self.request(rowCount=9000, columnCount=40),
                    self.request(rowCount=10, columnCount=40),
                ]
            )
        self.assertEqual(len(self.service.structural_kinds), before)
        self.assertEqual(self.service.grid["T"], (5000, 40))

    def test_growth_still_passes_the_guard(self) -> None:
        self.client.batch_update([self.request(rowCount=9000, columnCount=60)])
        self.assertEqual(self.service.grid["T"], (9000, 60))

    def test_an_unchanged_size_passes_the_guard(self) -> None:
        self.client.batch_update([self.request(rowCount=5000, columnCount=40)])
        self.assertEqual(self.service.grid["T"], (5000, 40))

    def test_freezing_a_header_row_is_not_a_shrink(self) -> None:
        """``format_header`` uses the same request kind and must still work."""
        self.client.format_header(self.sheet_id, columns=40, frozen_rows=1)

        self.assertEqual(self.service.frozen["T"], 1)
        self.assertEqual(self.service.grid["T"], (5000, 40))

    def test_a_tab_the_spreadsheet_does_not_hold_is_not_constrained(self) -> None:
        """A sheet being created in the same batch has nothing to shrink."""
        request = {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": 9999,
                    "gridProperties": {"rowCount": 5, "columnCount": 5},
                },
                "fields": "gridProperties.rowCount,gridProperties.columnCount",
            }
        }
        self.client.batch_update([request])  # must not raise

    def test_grid_of_reports_the_current_size(self) -> None:
        self.assertEqual(self.client.grid_of(self.sheet_id), (5000, 40))
        self.assertIsNone(self.client.grid_of(9999))


class TestExistingCallersRemainCompatible(unittest.TestCase):
    """The three callers of ``ensure_size``, exercised end to end."""

    def test_initialising_an_oversized_tab_does_not_shrink_it(self) -> None:
        """The exact production scenario, through the real code path.

        A MASTER_COMPANIES with the 16 headers it had, a grid far larger than
        its contents, and a schema that has since grown 8 columns. Before the
        fix this reduced the grid; now it appends the headers and leaves the
        grid alone.
        """
        from sheets.init import initialise
        from sheets.schema import MASTER_COMPANIES

        headers = [column.header for column in MASTER_COMPANIES.columns[:16]]
        data = [[f"Company {index}"] + [""] * 15 for index in range(1, 21)]
        service = FakeSheetsService({MASTER_COMPANIES.title: [headers, *data]})
        service.grid[MASTER_COMPANIES.title] = (25_458, 24)
        client = SheetsClient(service, "fake", sleep=lambda _s: None)

        report = initialise(client, only=[MASTER_COMPANIES.title])

        outcome = report.outcomes[0]
        self.assertEqual(outcome.action, "extended")
        self.assertEqual(len(outcome.appended_headers), 8)

        self.assertEqual(service.grid[MASTER_COMPANIES.title], (25_458, 24))
        self.assertEqual(len(service.tabs[MASTER_COMPANIES.title]), 21)
        self.assertEqual(service.tabs[MASTER_COMPANIES.title][1][0], "Company 1")
        self.assertEqual(service.tabs[MASTER_COMPANIES.title][20][0], "Company 20")
        self.assertEqual(service.destructive_requests(), [])

    def test_a_tabstore_append_past_the_grid_still_grows_it(self) -> None:
        """``TabStore._ensure_room`` must still be able to make room."""
        client, service = fixture()
        store = TabStore(client, CURRENT_JOBS)
        service.grid[CURRENT_JOBS.title] = (10, 18)
        client.invalidate()

        store.upsert(
            [{"job_key": f"j{index}"} for index in range(40)], key_field="job_key"
        )

        rows, _columns = service.grid[CURRENT_JOBS.title]
        self.assertGreaterEqual(rows, 41)
        self.assertEqual(len(store.read()), 40)

    def test_creating_a_spreadsheet_from_scratch_is_unaffected(self) -> None:
        """Every tab still comes up at its intended size."""
        from sheets.init import initialise
        from sheets.schema import ALL_TABS

        service = FakeSheetsService({"Sheet1": []})
        client = SheetsClient(service, "fake", sleep=lambda _s: None)

        initialise(client)

        for spec in ALL_TABS:
            rows, columns = service.grid[spec.title]
            self.assertGreaterEqual(rows, 2, spec.title)
            self.assertGreaterEqual(columns, len(spec.columns), spec.title)
        self.assertEqual(service.destructive_requests(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
