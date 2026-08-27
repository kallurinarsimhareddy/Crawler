"""The weekly run driven by the SQLite queue, behind ``--queue``.

The integration is deliberately narrow. The queue replaces one thing — *where
the next batch of companies comes from* — and nothing else. In particular the
in-run checkpoint still drives closure withholding, because that logic is
correct, was hard-won, and has its own regression tests; making the queue
responsible for it too would put the same decision in two places.

So these tests care about two questions above all:

* does the queue path crawl the right companies, survive a restart, and record
  failures with the right state; and
* is the default path **byte-for-byte the same behaviour** as before, so that
  omitting ``--queue`` changes nothing at all.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from crawler.platform_detector import Platform
from crawler.weekly_run import WeeklyRun
from sheets.companies import CompanyRepository as SheetCompanies
from store import Database, migrate
from store.queue import CrawlQueue, QueueState
from store.repositories import CompanyRepository, JobRepository
from tests.test_weekly_run import FakeEngine, fixture, posting, result


class QueueRunTest(unittest.TestCase):
    """A run backed by a temporary database and a fake spreadsheet."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.checkpoint = Path(self.directory.name) / "checkpoint.json"
        self.db_path = Path(self.directory.name) / "crawl.db"

        self.client, self.service = fixture()
        SheetCompanies(self.client).import_rows(
            [
                {"company": "Alpha", "website": "https://alpha.com"},
                {"company": "Bravo", "website": "https://bravo.com"},
                {"company": "Charlie", "website": "https://charlie.com"},
            ]
        )
        self.keys = ["domain:alpha.com", "domain:bravo.com", "domain:charlie.com"]

    def tearDown(self) -> None:
        self.directory.cleanup()

    def database(self) -> Database:
        """A migrated database on disk, so a restart can reopen it."""
        database = Database(self.db_path)
        migrate(database)
        return database

    def engine_for(self, *keys, jobs=1, error=None) -> FakeEngine:
        """An engine returning canned results for the given companies."""
        return FakeEngine(
            {
                key: result(
                    company=key,
                    platform=Platform.GREENHOUSE,
                    seed_url=f"https://boards.greenhouse.io/{key}",
                    seed_field="it_link",
                    jobs=[posting(f"Engineer {key}", f"https://x/{key}/1")] * jobs
                    if not error else [],
                    error=error,
                )
                for key in keys
            }
        )

    def run_queue(self, engine, database=None, **kwargs):
        """Execute one run through the queue path."""
        owned = database is None
        database = database or self.database()
        try:
            runner = WeeklyRun(
                self.client,
                engine=engine,
                checkpoint_path=self.checkpoint,
                batch_size=2,
                session_factory=lambda: None,
                database=database,
            )
            return runner.execute(use_queue=True, **kwargs)
        finally:
            if owned:
                database.close()


class TestTheQueuePathCrawls(QueueRunTest):
    """The basics: work comes from the queue and results go back to it."""

    def test_every_company_is_crawled(self) -> None:
        engine = self.engine_for(*self.keys)
        summary = self.run_queue(engine, run_id="run-1")

        self.assertEqual(len(engine.crawled), 3)
        self.assertEqual(summary.companies_succeeded, 3)

    def test_companies_are_loaded_into_the_database(self) -> None:
        self.run_queue(self.engine_for(*self.keys), run_id="run-1")

        database = self.database()
        try:
            self.assertEqual(CompanyRepository(database).count(), 3)
        finally:
            database.close()

    def test_the_queue_records_every_success(self) -> None:
        self.run_queue(self.engine_for(*self.keys), run_id="run-1")

        database = self.database()
        try:
            self.assertEqual(CrawlQueue(database).stats()[QueueState.SUCCEEDED], 3)
        finally:
            database.close()

    def test_a_failure_lands_in_a_failure_state(self) -> None:
        engine = self.engine_for(*self.keys, error="AdapterHttpError: HTTP 403")
        self.run_queue(engine, run_id="run-1")

        database = self.database()
        try:
            stats = CrawlQueue(database).stats()
            self.assertEqual(stats[QueueState.SUCCEEDED], 0)
            terminal = (stats[QueueState.FAILED] + stats[QueueState.BLOCKED]
                        + stats[QueueState.RETRY_WAIT])
            self.assertEqual(terminal, 3)
        finally:
            database.close()

    def test_postings_are_stored_in_the_database(self) -> None:
        self.run_queue(self.engine_for(*self.keys), run_id="run-1")

        database = self.database()
        try:
            self.assertGreater(JobRepository(database).count(), 0)
        finally:
            database.close()

    def test_the_sheet_is_still_written(self) -> None:
        """SQLite is the operational store, not a replacement for reporting."""
        self.run_queue(self.engine_for(*self.keys), run_id="run-1")
        row = SheetCompanies(self.client).store.read_index("company_key")[self.keys[0]]
        self.assertTrue(row.get("last_checked"))


class TestResumeThroughTheQueue(QueueRunTest):
    """A killed process resumes from SQLite, not from zero."""

    def test_a_restart_does_not_recrawl_finished_companies(self) -> None:
        database = self.database()
        first = self.engine_for(*self.keys)
        runner = WeeklyRun(
            self.client, engine=first, checkpoint_path=self.checkpoint,
            batch_size=1, session_factory=lambda: None, database=database,
        )
        original = runner._absorb

        def absorb(*args, **kwargs):
            """Stop after the first company, as a signal would."""
            original(*args, **kwargs)
            if len(first.crawled) >= 1:
                runner.request_stop()

        runner._absorb = absorb
        runner.execute(use_queue=True, run_id="run-1")
        database.close()

        # A brand new process, reopening the same file.
        second = self.engine_for(*self.keys)
        self.run_queue(second, run_id="run-1", resume=True)

        self.assertEqual(set(first.crawled) & set(second.crawled), set())
        self.assertEqual(len(first.crawled) + len(second.crawled), 3)

    def test_state_survives_the_process(self) -> None:
        self.run_queue(self.engine_for(*self.keys), run_id="run-1")

        reopened = self.database()
        try:
            self.assertEqual(CrawlQueue(reopened).stats()[QueueState.SUCCEEDED], 3)
        finally:
            reopened.close()

    def test_a_stale_claim_can_be_released_and_retried(self) -> None:
        database = self.database()
        try:
            CompanyRepository(database).upsert_many(
                [{"company_key": key, "company_name": key} for key in self.keys]
            )
            queue = CrawlQueue(database)
            queue.enqueue_all()
            queue.claim("dead-worker", limit=3)

            self.assertEqual(queue.stats()[QueueState.RUNNING], 3)
            self.assertEqual(queue.release_stale(lease_seconds=0), 3)
            self.assertEqual(queue.stats()[QueueState.PENDING], 3)
        finally:
            database.close()

    def test_a_second_run_requeues_finished_work(self) -> None:
        """Next week must crawl again; a resume must not."""
        self.run_queue(self.engine_for(*self.keys), run_id="run-1")

        second = self.engine_for(*self.keys)
        self.run_queue(second, run_id="run-2", resume=False)

        self.assertEqual(len(second.crawled), 3)


class TestClosureBehaviourIsUnchanged(QueueRunTest):
    """The rule the whole ledger rests on, checked through the queue path."""

    def test_a_blocked_company_does_not_close_its_jobs(self) -> None:
        self.run_queue(self.engine_for(*self.keys), run_id="run-1")

        blocked = self.engine_for(*self.keys, error="AdapterHttpError: HTTP 403")
        summary = self.run_queue(blocked, run_id="run-2", resume=False)

        self.assertEqual(len(summary.changes.closed_jobs), 0)
        self.assertGreater(summary.changes.skipped_closures, 0)

    def test_an_empty_board_does_close_its_jobs(self) -> None:
        """Read successfully and advertising nothing is real evidence."""
        self.run_queue(self.engine_for(*self.keys), run_id="run-1")

        empty = FakeEngine(
            {
                key: result(company=key, platform=Platform.GREENHOUSE,
                            seed_url=f"https://boards.greenhouse.io/{key}",
                            seed_field="it_link", jobs=[])
                for key in self.keys
            }
        )
        summary = self.run_queue(empty, run_id="run-2", resume=False)

        self.assertGreater(len(summary.changes.closed_jobs), 0)


class TestTheDefaultPathIsUntouched(unittest.TestCase):
    """Without ``--queue`` nothing about the run changes."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.checkpoint = Path(self.directory.name) / "checkpoint.json"
        self.client, self.service = fixture()
        SheetCompanies(self.client).import_rows(
            [{"company": "Alpha", "website": "https://alpha.com"}]
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_the_json_checkpoint_is_still_used(self) -> None:
        """A completed run archives its checkpoint, exactly as it always has."""
        WeeklyRun(
            self.client, engine=FakeEngine(), checkpoint_path=self.checkpoint,
            batch_size=5, session_factory=lambda: None,
        ).execute(run_id="run-1")

        archived = self.checkpoint.parent / "completed" / "run-1.json"
        self.assertTrue(archived.is_file())
        self.assertFalse(self.checkpoint.is_file())

    def test_no_database_is_created_without_the_flag(self) -> None:
        WeeklyRun(
            self.client, engine=FakeEngine(), checkpoint_path=self.checkpoint,
            batch_size=5, session_factory=lambda: None,
        ).execute(run_id="run-1")

        databases = list(Path(self.directory.name).glob("*.db"))
        self.assertEqual(databases, [])

    def test_the_default_run_still_succeeds(self) -> None:
        summary = WeeklyRun(
            self.client, engine=FakeEngine(), checkpoint_path=self.checkpoint,
            batch_size=5, session_factory=lambda: None,
        ).execute(run_id="run-1")

        self.assertEqual(summary.companies_attempted, 1)

    def test_the_flag_defaults_to_off(self) -> None:
        from crawler.weekly_run import _parse_args

        self.assertFalse(_parse_args([]).queue)

    def test_the_flag_can_be_switched_on(self) -> None:
        from crawler.weekly_run import _parse_args

        self.assertTrue(_parse_args(["--queue"]).queue)

    def test_worker_count_stays_configurable(self) -> None:
        from crawler.weekly_run import _parse_args

        for workers in (6, 15, 20):
            with self.subTest(workers=workers):
                self.assertEqual(_parse_args(["--workers", str(workers)]).workers,
                                 workers)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
