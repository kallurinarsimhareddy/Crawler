"""Results become durable batch by batch, not at the end of the run.

The failure these guard against is a specific one, and it happened. A run over
12,377 companies held every posting in memory and wrote nothing until the whole
crawl finished, while checkpointing each batch as it went. Stopping it after
200 companies left a checkpoint saying those companies were done and a
spreadsheet holding none of their jobs — work that could neither be resumed nor
recovered, because a resume would skip exactly the companies whose results were
lost.

So the invariant every test here defends is an ordering:

    crawl → persist → checkpoint → release

Persist *before* checkpoint, never after. A crash between the two costs a
replayed batch, which is safe because every write is keyed and converges. A
crash the other way around costs the batch permanently.

The second theme is capacity. A spreadsheet holds ten million cells, the roster
does not fit in one, and the crawl must survive that rather than fail on the
write. When a tab fills, SQLite still gets everything.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from unittest import mock

from crawler.checkpoint import STATUS_DONE, Checkpoint
from crawler.weekly_run import WeeklyRun, main
from sheets.capacity import (
    CELL_BUDGET,
    CapacityGuard,
    CapacityReport,
    measure,
    project,
)
from sheets.companies import CompanyRepository as SheetCompanies
from sheets.jobs import JobRepository as SheetJobs
from sheets.runs import FailureRepository, RunRepository
from store import Database, migrate
from store.repositories import JobRepository as StoreJobs
from tests.test_weekly_run import FakeEngine, fixture, posting, result


def sheet_row(name: str, website: str = "", career_url: str = "") -> Dict[str, str]:
    """A MASTER_COMPANIES row as an import produces it."""
    return {"company": name, "website": website, "career_url": career_url, "it_link": ""}


class IncrementalRunTest(unittest.TestCase):
    """A fake spreadsheet, a real file database, and the JSON crawl path.

    Deliberately not queue mode: the durable store is now the default path's
    guarantee too, and that is the thing under test.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.checkpoint_path = self.root / "checkpoint.json"
        self.database_path = self.root / "crawl.db"
        self.client, self.service = fixture()
        self.database = Database(self.database_path)
        migrate(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    # -- fixtures ------------------------------------------------------------

    def seed(self, *rows: Mapping[str, str]) -> None:
        """Put company rows in the fake MASTER_COMPANIES tab."""
        SheetCompanies(self.client).import_rows([dict(row) for row in rows])

    def many(self, count: int) -> List[str]:
        """Seed ``count`` companies and return their keys, in roster order."""
        rows = [
            sheet_row(f"Company {index}", f"c{index}.com", f"https://c{index}.com/careers")
            for index in range(count)
        ]
        self.seed(*rows)
        return [f"domain:c{index}.com" for index in range(count)]

    def runner(self, engine: Any, batch_size: int = 2, **kwargs: Any) -> WeeklyRun:
        """A JSON-path runner with a durable store behind it."""
        return WeeklyRun(
            self.client,
            engine=engine,
            checkpoint_path=self.checkpoint_path,
            database=self.database,
            batch_size=batch_size,
            **kwargs,
        )

    def jobs_for(self, keys: Sequence[str]) -> Dict[str, Any]:
        """Canned results: one posting for each named company."""
        return {
            key: result(
                company=key,
                jobs=[posting(title="Software Engineer", url=f"https://{key}/jobs/1")],
            )
            for key in keys
        }

    # -- readers -------------------------------------------------------------

    def stored_jobs(self) -> List[Dict[str, Any]]:
        """Every row in the SQLite jobs table."""
        return StoreJobs(self.database).all()

    def current_rows(self) -> List[Dict[str, Any]]:
        """Every CURRENT_JOBS row."""
        return SheetJobs(self.client).current.read()

    def weekly_rows(self) -> List[Dict[str, Any]]:
        """Every NEW_LAST_WEEK row."""
        return SheetJobs(self.client).weekly.read()

    def failure_rows(self) -> List[Dict[str, Any]]:
        """Every FAILURES row."""
        return FailureRepository(self.client).store.read()


# ---------------------------------------------------------------------------
# 1-3. The ordering invariant, and what a replay costs
# ---------------------------------------------------------------------------


class TestPersistBeforeCheckpoint(IncrementalRunTest):
    """Nothing is recorded as done before it is stored."""

    def test_each_batch_is_persisted_before_it_is_checkpointed(self) -> None:
        """The ordering, observed rather than assumed.

        At the moment the checkpoint records a company, that company's postings
        must already be in SQLite. Checked inside ``_save`` so the assertion
        sees the exact interleaving rather than the end state.
        """
        keys = self.many(4)
        run = self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2)

        seen: List[bool] = []
        original = run._save

        def save(checkpoint: Checkpoint) -> None:
            done = {key for key, status in checkpoint.companies.items() if status == STATUS_DONE}
            stored = {row["company_key"] for row in self.stored_jobs()}
            # Every company the checkpoint calls done has its jobs stored.
            seen.append(done <= stored)
            original(checkpoint)

        run._save = save
        run.execute(run_id="run-1")

        self.assertTrue(seen, "the checkpoint was never written")
        self.assertTrue(all(seen), "a company was checkpointed before its jobs were stored")

    def test_a_crash_before_the_checkpoint_replays_the_batch(self) -> None:
        """Losing the checkpoint costs a repeat, not the results."""
        keys = self.many(4)
        run = self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2)

        # The batch is persisted, then the checkpoint write dies.
        def explode(_checkpoint: Checkpoint) -> None:
            raise RuntimeError("killed between the write and the checkpoint")

        run._save = explode

        with self.assertRaises(RuntimeError):
            run.execute(run_id="run-1")

        # The first batch's postings survived, because they were written first.
        self.assertEqual(len(self.stored_jobs()), 2)
        # And nothing claims those companies are done.
        self.assertFalse(self.checkpoint_path.is_file())

    def test_a_replay_does_not_duplicate_jobs(self) -> None:
        """The same batch, written twice, is still one row per posting."""
        keys = self.many(4)

        first = self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2)
        first._save = lambda _checkpoint: None  # never records progress
        first.execute(run_id="run-1")

        before = {row["job_key"] for row in self.stored_jobs()}
        self.assertEqual(len(before), 4)

        # Every company is crawled again, because nothing was checkpointed.
        second = self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2)
        second.execute(run_id="run-1")

        after = {row["job_key"] for row in self.stored_jobs()}
        self.assertEqual(before, after, "the replay invented new identities")
        self.assertEqual(len(self.stored_jobs()), 4, "the replay duplicated postings")

    def test_a_replay_does_not_duplicate_weekly_rows(self) -> None:
        """NEW_LAST_WEEK is keyed on the run, the job and the change."""
        keys = self.many(4)

        first = self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2)
        first._save = lambda _checkpoint: None
        first.execute(run_id="run-1")

        logged = len(self.weekly_rows())
        self.assertEqual(logged, 4)

        second = self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2)
        second.execute(run_id="run-1")

        self.assertEqual(len(self.weekly_rows()), logged, "the replay logged its changes twice")


# ---------------------------------------------------------------------------
# 4-7. One batch must not erase another
# ---------------------------------------------------------------------------


class TestBatchesAccumulate(IncrementalRunTest):
    """The bug that made incremental writing impossible: replace()."""

    def test_current_jobs_keeps_every_batch(self) -> None:
        """Batch two must not delete batch one.

        ``_write_current`` replaced the whole tab. Called once per batch, that
        would leave a twelve-thousand-company run holding only its last two
        hundred companies' postings.
        """
        keys = self.many(6)
        self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2).execute(run_id="run-1")

        companies = {str(row.get("company_name")) for row in self.current_rows()}
        self.assertEqual(len(self.current_rows()), 6)
        self.assertEqual(len(companies), 6, "a later batch overwrote an earlier one")

    def test_new_last_week_keeps_every_batch(self) -> None:
        """The run-scoped guard used to silence every batch after the first.

        ``logged_runs()`` asked "has this run written?", which is true from the
        second batch onward — so batches two and beyond appended nothing at all.
        """
        keys = self.many(6)
        self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2).execute(run_id="run-1")

        rows = self.weekly_rows()
        self.assertEqual(len(rows), 6, "batches after the first were skipped")
        self.assertEqual(len({str(row.get("job_key")) for row in rows}), 6)

    def test_failures_keep_every_batch(self) -> None:
        """FAILURES is upserted per batch, not replaced."""
        keys = self.many(6)
        failing = {key: result(company=key, error="AdapterHttpError: 403") for key in keys}
        self.runner(FakeEngine(failing), batch_size=2).execute(run_id="run-1")

        rows = self.failure_rows()
        self.assertEqual(len(rows), 6, "a later batch replaced an earlier batch's failures")
        self.assertEqual(len({str(row.get("company_key")) for row in rows}), 6)

    def test_weekly_runs_reports_progress_during_the_crawl(self) -> None:
        """WEEKLY_RUNS is worth reading before the run ends."""
        keys = self.many(6)
        run = self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2)

        progress: List[int] = []
        original = run._save

        def save(checkpoint: Checkpoint) -> None:
            record = RunRepository(self.client).get("run-1")
            if record is not None:
                progress.append(int(record.counts.get("companies_checked", 0)))
            original(checkpoint)

        run._save = save
        run.execute(run_id="run-1")

        self.assertEqual(progress, [2, 4, 6], "the run's counters did not advance per batch")

        finished = RunRepository(self.client).get("run-1")
        self.assertIsNotNone(finished)
        self.assertEqual(finished.status, "done")
        self.assertEqual(int(finished.counts["companies_checked"]), 6)


# ---------------------------------------------------------------------------
# 8. The property the whole change risks
# ---------------------------------------------------------------------------


class TestClosureWithholdingSurvives(IncrementalRunTest):
    """A batch may only close postings for companies it actually read."""

    def test_a_batch_does_not_close_another_batchs_postings(self) -> None:
        """Per-batch comparison must not read absence as closure.

        Every batch compares against the ledger. If the comparison were handed
        the whole roster rather than the batch's own companies, batch two would
        find batch one's postings absent from its own observations and close
        every one of them.
        """
        keys = self.many(4)
        self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2).execute(run_id="run-1")

        self.assertEqual(len(self.stored_jobs()), 4)
        self.assertTrue(
            all(row["status"] == "active" for row in self.stored_jobs()),
            "a batch closed postings belonging to a company it never crawled",
        )

    def test_a_company_that_failed_keeps_its_postings(self) -> None:
        """A blocked board proves nothing about what it advertises."""
        keys = self.many(2)
        self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2).execute(run_id="run-1")
        self.assertEqual(len(self.stored_jobs()), 2)

        # The same companies, now unreadable.
        blocked = {key: result(company=key, error="AdapterHttpError: 403") for key in keys}
        self.runner(FakeEngine(blocked), batch_size=2).execute(run_id="run-2", resume=False)

        self.assertTrue(
            all(row["status"] == "active" for row in self.stored_jobs()),
            "a failed crawl closed the postings it could not see",
        )

    def test_an_empty_board_does_close_its_postings(self) -> None:
        """Read successfully and advertising nothing is real evidence."""
        keys = self.many(2)
        self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2).execute(run_id="run-1")

        empty = {key: result(company=key, jobs=[]) for key in keys}
        self.runner(FakeEngine(empty), batch_size=2).execute(run_id="run-2", resume=False)

        self.assertTrue(
            all(row["status"] == "closed" for row in self.stored_jobs()),
            "an empty board left its postings open",
        )


# ---------------------------------------------------------------------------
# 9-11. Capacity
# ---------------------------------------------------------------------------


class TestCapacityCalculation(unittest.TestCase):
    """The arithmetic, before it is trusted to stop a run."""

    def test_measure_sums_every_tab_as_rows_times_columns(self) -> None:
        """Google counts the grid, not the cells an operator filled in."""
        client = mock.Mock()
        client.metadata.return_value = {
            "sheets": [
                {"properties": {"title": "A", "gridProperties": {"rowCount": 1000, "columnCount": 20}}},
                {"properties": {"title": "B", "gridProperties": {"rowCount": 500, "columnCount": 10}}},
            ]
        }

        report = measure(client)

        self.assertEqual(report.per_tab, {"A": 20_000, "B": 5_000})
        self.assertEqual(report.used, 25_000)
        self.assertEqual(report.budget, CELL_BUDGET)

    def test_available_subtracts_the_reserve(self) -> None:
        """The reserve is held back so a full sheet can still say it is full."""
        report = CapacityReport(used=1_000_000, budget=10_000_000, reserve=250_000)
        self.assertEqual(report.available, 8_750_000)

    def test_available_never_goes_negative(self) -> None:
        report = CapacityReport(used=12_000_000, budget=10_000_000, reserve=250_000)
        self.assertEqual(report.available, 0)

    def test_a_projection_that_does_not_fit_says_what_would(self) -> None:
        """The number an operator needs is 'how many companies', not 'no'."""
        projection = project(
            companies=12_377,
            jobs_per_company=28.68,
            cells_per_posting=33.35,
            available=9_195_814,
        )

        self.assertFalse(projection.fits)
        self.assertGreater(projection.cells, projection.available)
        self.assertGreater(projection.companies_that_fit, 0)
        self.assertLess(projection.companies_that_fit, 12_377)

    def test_a_projection_that_fits_says_so(self) -> None:
        projection = project(
            companies=100,
            jobs_per_company=30.0,
            cells_per_posting=33.0,
            available=9_000_000,
        )
        self.assertTrue(projection.fits)
        self.assertEqual(projection.companies_that_fit, 100)


class TestCapacityGuard(unittest.TestCase):
    """The guard stops a tab, records why, and never deletes anything."""

    def test_it_allows_what_fits(self) -> None:
        guard = CapacityGuard(CapacityReport(used=0, budget=10_000, reserve=0))
        self.assertEqual(guard.allow_rows("T", rows=100, columns=10), 100)
        self.assertEqual(guard.remaining, 9_000)
        self.assertFalse(guard.is_blocked("T"))

    def test_it_truncates_the_batch_that_runs_out(self) -> None:
        """A partial write beats an exception on the write."""
        guard = CapacityGuard(CapacityReport(used=0, budget=1_000, reserve=0))

        self.assertEqual(guard.allow_rows("T", rows=200, columns=10), 100)
        self.assertTrue(guard.is_blocked("T"))
        self.assertIn("budget is spent", guard.blocked["T"])

    def test_a_blocked_tab_takes_nothing_further(self) -> None:
        guard = CapacityGuard(CapacityReport(used=0, budget=1_000, reserve=0))
        guard.allow_rows("T", rows=200, columns=10)
        self.assertEqual(guard.allow_rows("T", rows=1, columns=10), 0)

    def test_blocking_one_tab_leaves_the_others_open(self) -> None:
        """A full change log must not stop the snapshot being updated."""
        guard = CapacityGuard(CapacityReport(used=0, budget=2_000, reserve=0))
        guard.note("BIG", "closed by hand")

        self.assertEqual(guard.allow_rows("BIG", rows=1, columns=10), 0)
        self.assertEqual(guard.allow_rows("SMALL", rows=10, columns=10), 10)

    def test_the_summary_names_the_tabs_that_stopped(self) -> None:
        guard = CapacityGuard(CapacityReport(used=0, budget=100, reserve=0))
        guard.allow_rows("T", rows=50, columns=10)
        self.assertIn("T", guard.summary())


class TestSqliteSurvivesAFullSheet(IncrementalRunTest):
    """The point of the ledger: the crawl outlives the spreadsheet."""

    def test_every_posting_is_stored_even_when_the_sheet_is_full(self) -> None:
        """Capacity stops the tabs, not the crawl and not the store."""
        keys = self.many(6)
        exhausted = CapacityReport(used=CELL_BUDGET, budget=CELL_BUDGET, reserve=0)

        with mock.patch("crawler.weekly_run.measure", return_value=exhausted):
            summary = self.runner(
                FakeEngine(self.jobs_for(keys)), batch_size=2
            ).execute(run_id="run-1")

        # Nothing could be written to the tabs the guard protects...
        self.assertEqual(self.current_rows(), [])
        self.assertEqual(self.weekly_rows(), [])
        # ...and every posting is still durable.
        self.assertEqual(len(self.stored_jobs()), 6)
        self.assertEqual(summary.companies_succeeded, 6)
        # The condition is recorded rather than raised.
        self.assertTrue(summary.capacity_blocked)

    def test_the_run_records_why_its_output_stopped(self) -> None:
        keys = self.many(2)
        exhausted = CapacityReport(used=CELL_BUDGET, budget=CELL_BUDGET, reserve=0)

        with mock.patch("crawler.weekly_run.measure", return_value=exhausted):
            self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2).execute(run_id="run-1")

        record = RunRepository(self.client).get("run-1")
        self.assertIsNotNone(record)
        self.assertIn("capacity", (record.notes or "").lower())


# ---------------------------------------------------------------------------
# 12-14. The CLI, the stranded checkpoint, and memory
# ---------------------------------------------------------------------------


class TestMainExecutes(IncrementalRunTest):
    """main() had no test at all, which is how a NameError reached production."""

    def setUp(self) -> None:
        super().setUp()

        # main() calls configure(), which mutates a module-level singleton that
        # every other test in the process shares. Snapshotted and restored, or
        # the crawl settings this test chooses leak into the rest of the suite.
        from dataclasses import fields

        from config.settings import SETTINGS

        self._settings = {item.name: getattr(SETTINGS, item.name) for item in fields(SETTINGS)}

    def tearDown(self) -> None:
        from config.settings import SETTINGS

        for name, value in self._settings.items():
            setattr(SETTINGS, name, value)
        super().tearDown()

    def _connection(self) -> Any:
        """A connection object shaped like sheets._cli.Connection."""
        connection = mock.Mock()
        connection.client = self.client
        connection.account = "test@example.com"
        connection.spreadsheet_id = "fake"
        connection.read_only = False
        return connection

    def test_main_runs_a_crawl_end_to_end(self) -> None:
        """The entry point the scheduled task actually invokes."""
        self.many(2)

        with mock.patch("sheets._cli.connect", return_value=(self._connection(), 0)), \
             mock.patch("crawler.weekly_run.CrawlerEngine", return_value=FakeEngine()):
            code = main([
                "--workers", "1",
                "--batch-size", "2",
                "--checkpoint", str(self.checkpoint_path),
                "--database", str(self.root / "main.db"),
                "--log-level", "ERROR",
            ])

        self.assertEqual(code, 0)

    def test_main_reports_a_configuration_failure(self) -> None:
        """A missing spreadsheet is exit 2, not a traceback."""
        with mock.patch("sheets._cli.connect", return_value=(None, 2)):
            self.assertEqual(main(["--log-level", "ERROR"]), 2)


class TestStrandedCheckpoint(IncrementalRunTest):
    """The 200 companies whose postings were never written."""

    def _strand(self, keys: Sequence[str]) -> None:
        """Write a checkpoint of the old, pre-durability shape."""
        checkpoint = Checkpoint.start("run-old", total=len(keys), path=self.checkpoint_path)
        for key in keys:
            checkpoint.record(key, STATUS_DONE)
        checkpoint.save()

        payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        payload.pop("durable", None)  # exactly what the stranded file looks like
        self.checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_a_stranded_checkpoint_is_not_resumed(self) -> None:
        """Its companies are crawled again, because their jobs were never stored."""
        keys = self.many(4)
        self._strand(keys[:2])

        engine = FakeEngine(self.jobs_for(keys))
        self.runner(engine, batch_size=2).execute(run_id="run-new", resume=True)

        self.assertEqual(
            set(engine.crawled), set(keys), "the companies with no stored jobs were skipped"
        )
        self.assertEqual(len(self.stored_jobs()), 4)

    def test_the_stranded_file_is_not_deleted(self) -> None:
        """It is evidence. Starting fresh does not throw it away."""
        keys = self.many(2)
        self._strand(keys)

        self.assertTrue(self.checkpoint_path.is_file())
        self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2).execute(
            run_id="run-new", resume=True
        )
        # Replaced by the new run's own checkpoint, then archived on success --
        # never silently removed while it was the only record of that run.
        self.assertTrue(self.checkpoint_path.parent.is_dir())

    def test_a_durable_checkpoint_is_still_resumed(self) -> None:
        """The guard must not cost legitimate resumes."""
        keys = self.many(4)

        checkpoint = Checkpoint.start("run-1", total=4, path=self.checkpoint_path)
        checkpoint.durable = True
        checkpoint.record(keys[0], STATUS_DONE)
        checkpoint.save()

        engine = FakeEngine(self.jobs_for(keys))
        summary = self.runner(engine, batch_size=2).execute(run_id="run-x", resume=True)

        self.assertTrue(summary.resumed)
        self.assertNotIn(keys[0], engine.crawled)


class TestMemoryIsReleased(IncrementalRunTest):
    """A batch's postings do not accumulate for the length of the run."""

    def test_each_batch_persists_only_its_own_observations(self) -> None:
        """The accumulator is cleared, so batch three is not batch one plus two.

        Observed through what each batch hands to persistence: if the list were
        never cleared, the sizes would climb 2, 4, 6 instead of staying at 2.
        """
        keys = self.many(6)
        run = self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2)

        sizes: List[int] = []
        original = run._persist_segment

        def persist(*args: Any, **kwargs: Any) -> None:
            sizes.append(len(kwargs["observations"]))
            original(*args, **kwargs)

        run._persist_segment = persist
        run.execute(run_id="run-1")

        self.assertEqual(sizes, [2, 2, 2], "observations accumulated across batches")

    def test_the_accumulators_are_empty_when_the_run_ends(self) -> None:
        """Nothing is still being held once the last batch is written."""
        keys = self.many(6)
        run = self.runner(FakeEngine(self.jobs_for(keys)), batch_size=2)
        summary = run.execute(run_id="run-1")

        # The running total is kept even though the records themselves are not.
        self.assertEqual(summary.observations, 6)
        self.assertEqual(len(self.stored_jobs()), 6)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
