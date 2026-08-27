"""The SQLite persistence layer: schema, repositories and the crawl queue.

Every test runs against an in-memory or temporary database. Nothing here
reaches Google, a website, or Chromium.

The rules these guard are the ones that make a 100,000-company run survivable:
initialisation is idempotent, two workers can never hold the same company, and
a worker that dies does not strand its claim forever.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from store import Database, migrate
from store.queue import CrawlQueue, QueueState
from store.repositories import CompanyRepository, JobRepository


def memory() -> Database:
    """A migrated, empty database held in memory."""
    database = Database(":memory:")
    migrate(database)
    return database


def company(key: str = "domain:acme.com", **overrides) -> dict:
    """A company record as the sheet supplies it."""
    record = {
        "company_key": key,
        "company_name": "Acme",
        "website": "https://acme.com",
        "career_url": "https://acme.com/careers",
        "it_link": "",
        "platform": "",
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# Schema and migration
# ---------------------------------------------------------------------------


class TestMigration(unittest.TestCase):
    """Initialisation must be safe to run over and over."""

    def test_migration_creates_the_tables(self) -> None:
        database = memory()
        tables = {
            row["name"]
            for row in database.query(
                "SELECT name FROM sqlite_master WHERE type = 'query_type'".replace(
                    "query_type", "table"
                )
            )
        }
        for expected in ("companies", "jobs", "crawl_queue", "discovery_queue",
                         "crawl_attempts", "schema_version"):
            self.assertIn(expected, tables)

    def test_migration_is_idempotent(self) -> None:
        database = memory()
        migrate(database)
        migrate(database)
        version = database.one("SELECT MAX(version) AS v FROM schema_version")
        self.assertIsNotNone(version)

    def test_running_it_twice_preserves_data(self) -> None:
        database = memory()
        CompanyRepository(database).upsert_many([company()])
        migrate(database)
        self.assertEqual(CompanyRepository(database).count(), 1)

    def test_indexes_exist_for_the_hot_paths(self) -> None:
        database = memory()
        names = {
            row["name"]
            for row in database.query(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        # Named explicitly so a dropped index is a failing test, not a slow run.
        for expected in (
            "idx_companies_domain", "idx_companies_platform",
            "idx_jobs_company", "idx_jobs_url_key", "idx_jobs_status",
            "idx_queue_state", "idx_queue_next_attempt",
        ):
            self.assertIn(expected, names)


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


class TestCompanyRepository(unittest.TestCase):
    """Companies are keyed on the crawler's own company_key."""

    def setUp(self) -> None:
        self.database = memory()
        self.companies = CompanyRepository(self.database)

    def test_a_company_round_trips(self) -> None:
        self.companies.upsert_many([company()])
        stored = self.companies.get("domain:acme.com")
        self.assertEqual(stored["company_name"], "Acme")
        self.assertEqual(stored["website"], "https://acme.com")

    def test_upsert_is_idempotent(self) -> None:
        self.companies.upsert_many([company()])
        self.companies.upsert_many([company()])
        self.assertEqual(self.companies.count(), 1)

    def test_an_existing_it_link_is_never_cleared_by_a_blank(self) -> None:
        """The rule that protects an operator's own entry."""
        self.companies.upsert_many([company(it_link="https://boards.greenhouse.io/acme")])
        self.companies.upsert_many([company(it_link="")])
        self.assertEqual(
            self.companies.get("domain:acme.com")["it_link"],
            "https://boards.greenhouse.io/acme",
        )

    def test_the_company_key_is_never_rewritten(self) -> None:
        self.companies.upsert_many([company()])
        self.companies.upsert_many([company(company_name="Acme Renamed")])
        self.assertEqual(self.companies.get("domain:acme.com")["company_key"],
                         "domain:acme.com")

    def test_the_domain_is_derived_and_indexed(self) -> None:
        self.companies.upsert_many([company()])
        self.assertEqual(self.companies.get("domain:acme.com")["domain"], "acme.com")

    def test_companies_stream_in_batches(self) -> None:
        """A 100,000-row table must never be pulled into memory at once."""
        self.companies.upsert_many(
            [company(key=f"domain:c{n}.com", website=f"https://c{n}.com")
             for n in range(250)]
        )
        seen = list(self.companies.stream(batch_size=50))
        self.assertEqual(len(seen), 250)


# ---------------------------------------------------------------------------
# The crawl queue
# ---------------------------------------------------------------------------


class TestQueueBasics(unittest.TestCase):
    """Enqueue, claim, finish."""

    def setUp(self) -> None:
        self.database = memory()
        CompanyRepository(self.database).upsert_many(
            [company(key=f"domain:c{n}.com") for n in range(5)]
        )
        self.queue = CrawlQueue(self.database)

    def test_enqueue_adds_every_company_as_pending(self) -> None:
        self.assertEqual(self.queue.enqueue_all(), 5)
        self.assertEqual(self.queue.stats()[QueueState.PENDING], 5)

    def test_enqueue_is_idempotent(self) -> None:
        self.queue.enqueue_all()
        self.assertEqual(self.queue.enqueue_all(), 0)
        self.assertEqual(self.queue.stats()[QueueState.PENDING], 5)

    def test_claiming_moves_work_out_of_pending(self) -> None:
        self.queue.enqueue_all()
        claimed = self.queue.claim("worker-1", limit=2)
        self.assertEqual(len(claimed), 2)
        self.assertEqual(self.queue.stats()[QueueState.PENDING], 3)
        self.assertEqual(self.queue.stats()[QueueState.RUNNING], 2)

    def test_an_empty_queue_yields_nothing(self) -> None:
        self.assertEqual(self.queue.claim("worker-1", limit=5), [])

    def test_success_marks_the_company_succeeded(self) -> None:
        self.queue.enqueue_all()
        item = self.queue.claim("worker-1", limit=1)[0]
        self.queue.succeed(item.company_key, jobs=7)
        self.assertEqual(self.queue.stats()[QueueState.SUCCEEDED], 1)

    def test_success_records_the_job_count_and_timestamp(self) -> None:
        self.queue.enqueue_all()
        item = self.queue.claim("worker-1", limit=1)[0]
        self.queue.succeed(item.company_key, jobs=7)
        row = self.queue.get(item.company_key)
        self.assertEqual(row["jobs_found"], 7)
        self.assertTrue(row["last_success_at"])


class TestAtomicClaiming(unittest.TestCase):
    """Two workers must never hold the same company."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "crawl.db"
        database = Database(self.path)
        migrate(database)
        CompanyRepository(database).upsert_many(
            [company(key=f"domain:c{n}.com") for n in range(60)]
        )
        CrawlQueue(database).enqueue_all()
        database.close()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_one_company_is_never_claimed_twice(self) -> None:
        database = Database(self.path)
        queue = CrawlQueue(database)
        first = {item.company_key for item in queue.claim("worker-1", limit=30)}
        second = {item.company_key for item in queue.claim("worker-2", limit=30)}

        self.assertEqual(first & second, set())
        self.assertEqual(len(first | second), 60)
        database.close()

    def test_concurrent_workers_partition_the_queue(self) -> None:
        """The real test: eight threads racing for the same rows."""
        taken: list = []
        lock = threading.Lock()

        def worker(name: str) -> None:
            """Claim repeatedly until the queue is empty."""
            local = Database(self.path)
            queue = CrawlQueue(local)
            while True:
                batch = queue.claim(name, limit=3)
                if not batch:
                    break
                with lock:
                    taken.extend(item.company_key for item in batch)
            local.close()

        threads = [threading.Thread(target=worker, args=(f"w{n}",)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(taken), 60, "a company was lost")
        self.assertEqual(len(set(taken)), 60, "a company was claimed twice")


class TestStaleClaims(unittest.TestCase):
    """A worker that dies must not strand its company."""

    def setUp(self) -> None:
        self.database = memory()
        CompanyRepository(self.database).upsert_many([company()])
        self.queue = CrawlQueue(self.database)
        self.queue.enqueue_all()

    def test_a_fresh_claim_is_not_reclaimed(self) -> None:
        self.queue.claim("worker-1", limit=1)
        self.assertEqual(self.queue.release_stale(lease_seconds=3600), 0)

    def test_an_expired_claim_returns_to_pending(self) -> None:
        self.queue.claim("worker-1", limit=1)
        released = self.queue.release_stale(lease_seconds=0)
        self.assertEqual(released, 1)
        self.assertEqual(self.queue.stats()[QueueState.PENDING], 1)

    def test_a_released_company_can_be_claimed_again(self) -> None:
        self.queue.claim("worker-1", limit=1)
        self.queue.release_stale(lease_seconds=0)
        self.assertEqual(len(self.queue.claim("worker-2", limit=1)), 1)

    def test_a_heartbeat_keeps_a_long_crawl_alive(self) -> None:
        item = self.queue.claim("worker-1", limit=1)[0]
        self.queue.heartbeat(item.company_key)
        self.assertEqual(self.queue.release_stale(lease_seconds=3600), 0)

    def test_releasing_stale_claims_is_idempotent(self) -> None:
        self.queue.claim("worker-1", limit=1)
        self.queue.release_stale(lease_seconds=0)
        self.assertEqual(self.queue.release_stale(lease_seconds=0), 0)


class TestQueueRetryStates(unittest.TestCase):
    """Failure moves a company to retry_wait, blocked or failed."""

    def setUp(self) -> None:
        self.database = memory()
        CompanyRepository(self.database).upsert_many([company()])
        self.queue = CrawlQueue(self.database)
        self.queue.enqueue_all()
        self.queue.claim("worker-1", limit=1)

    def test_a_retryable_failure_waits(self) -> None:
        self.queue.fail("domain:acme.com", reason="network failure",
                        retry_in=60.0, retryable=True)
        self.assertEqual(self.queue.stats()[QueueState.RETRY_WAIT], 1)

    def test_a_waiting_company_is_not_claimable_yet(self) -> None:
        self.queue.fail("domain:acme.com", reason="429", retry_in=3600.0, retryable=True)
        self.assertEqual(self.queue.claim("worker-2", limit=5), [])

    def test_a_waiting_company_becomes_claimable_once_due(self) -> None:
        self.queue.fail("domain:acme.com", reason="429", retry_in=0.0, retryable=True)
        self.assertEqual(len(self.queue.claim("worker-2", limit=5)), 1)

    def test_a_permanent_failure_is_blocked_not_retried(self) -> None:
        self.queue.fail("domain:acme.com", reason="captcha",
                        retry_in=0.0, retryable=False, blocked=True)
        self.assertEqual(self.queue.stats()[QueueState.BLOCKED], 1)
        self.assertEqual(self.queue.claim("worker-2", limit=5), [])

    def test_attempts_accumulate(self) -> None:
        """setUp already claimed once, so three more rounds make four."""
        for _ in range(3):
            self.queue.fail("domain:acme.com", reason="timeout",
                            retry_in=0.0, retryable=True)
            self.queue.claim("worker-1", limit=1)
        self.assertEqual(self.queue.get("domain:acme.com")["attempts"], 4)

    def test_every_attempt_is_recorded_for_forensics(self) -> None:
        self.queue.fail("domain:acme.com", reason="timeout",
                        retry_in=0.0, retryable=True)
        attempts = self.database.query(
            "SELECT * FROM crawl_attempts WHERE company_key = ?", ("domain:acme.com",)
        )
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["reason"], "timeout")


class TestResumeAfterCrash(unittest.TestCase):
    """State lives on disk, so a killed process resumes rather than restarts."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "crawl.db"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def open(self) -> Database:
        """A connection to the on-disk database."""
        database = Database(self.path)
        migrate(database)
        return database

    def test_progress_survives_a_process_restart(self) -> None:
        first = self.open()
        CompanyRepository(first).upsert_many(
            [company(key=f"domain:c{n}.com") for n in range(10)]
        )
        queue = CrawlQueue(first)
        queue.enqueue_all()
        for item in queue.claim("worker-1", limit=4):
            queue.succeed(item.company_key, jobs=1)
        first.close()  # the "crash"

        second = self.open()
        resumed = CrawlQueue(second)
        self.assertEqual(resumed.stats()[QueueState.SUCCEEDED], 4)
        self.assertEqual(resumed.stats()[QueueState.PENDING], 6)
        second.close()

    def test_a_crash_mid_claim_recovers_through_the_lease(self) -> None:
        first = self.open()
        CompanyRepository(first).upsert_many(
            [company(key=f"domain:c{n}.com") for n in range(4)]
        )
        queue = CrawlQueue(first)
        queue.enqueue_all()
        queue.claim("doomed-worker", limit=4)
        first.close()  # dies holding all four

        second = self.open()
        resumed = CrawlQueue(second)
        self.assertEqual(resumed.stats()[QueueState.RUNNING], 4)
        self.assertEqual(resumed.release_stale(lease_seconds=0), 4)
        self.assertEqual(resumed.stats()[QueueState.PENDING], 4)
        second.close()

    def test_only_unfinished_work_is_re_run(self) -> None:
        first = self.open()
        CompanyRepository(first).upsert_many(
            [company(key=f"domain:c{n}.com") for n in range(6)]
        )
        queue = CrawlQueue(first)
        queue.enqueue_all()
        done = queue.claim("worker-1", limit=2)
        for item in done:
            queue.succeed(item.company_key, jobs=1)
        first.close()

        second = self.open()
        resumed = CrawlQueue(second)
        again = {item.company_key for item in resumed.claim("worker-2", limit=99)}
        self.assertEqual(again & {item.company_key for item in done}, set())
        second.close()


# ---------------------------------------------------------------------------
# Jobs and deduplication
# ---------------------------------------------------------------------------


class TestJobDeduplication(unittest.TestCase):
    """The database is the last line of defence against duplicates."""

    def setUp(self) -> None:
        self.database = memory()
        CompanyRepository(self.database).upsert_many([company()])
        self.jobs = JobRepository(self.database)

    def posting(self, **overrides) -> dict:
        """One posting, in the shape the repository stores."""
        record = {
            "company_key": "domain:acme.com",
            "job_title": "Software Engineer",
            "job_url": "https://boards.greenhouse.io/acme/jobs/1",
            "location": "Austin, TX",
            "platform": "Greenhouse",
            "job_id": "1",
        }
        record.update(overrides)
        return record

    def test_the_same_posting_twice_stores_once(self) -> None:
        self.jobs.record_many([self.posting(), self.posting()])
        self.assertEqual(self.jobs.count(), 1)

    def test_the_same_posting_across_two_crawls_stores_once(self) -> None:
        self.jobs.record_many([self.posting()])
        self.jobs.record_many([self.posting()])
        self.assertEqual(self.jobs.count(), 1)

    def test_pagination_repeats_are_collapsed(self) -> None:
        page_one = [self.posting(job_id=str(n), job_url=f"https://x/{n}")
                    for n in range(5)]
        page_two = page_one[-2:] + [
            self.posting(job_id=str(n), job_url=f"https://x/{n}") for n in (5, 6)
        ]
        self.jobs.record_many(page_one + page_two)
        self.assertEqual(self.jobs.count(), 7)

    def test_tracking_parameters_do_not_create_a_second_job(self) -> None:
        """The same posting linked from a campaign is the same posting."""
        self.jobs.record_many([self.posting()])
        self.jobs.record_many([
            self.posting(job_url="https://boards.greenhouse.io/acme/jobs/1?utm_source=x")
        ])
        self.assertEqual(self.jobs.count(), 1)

    def test_two_genuinely_different_postings_are_kept_apart(self) -> None:
        self.jobs.record_many([
            self.posting(job_id="1", job_url="https://x/1"),
            self.posting(job_id="2", job_url="https://x/2", job_title="Data Engineer"),
        ])
        self.assertEqual(self.jobs.count(), 2)

    def test_two_companies_on_one_ats_do_not_collide(self) -> None:
        """Shared vendor infrastructure must not merge two employers' jobs."""
        CompanyRepository(self.database).upsert_many(
            [company(key="domain:other.com", website="https://other.com")]
        )
        self.jobs.record_many([
            self.posting(company_key="domain:acme.com"),
            self.posting(company_key="domain:other.com"),
        ])
        self.assertEqual(self.jobs.count(), 2)

    def test_a_re_crawl_updates_last_seen_rather_than_inserting(self) -> None:
        self.jobs.record_many([self.posting()])
        first = self.jobs.all()[0]
        self.jobs.record_many([self.posting(job_title="Software Engineer II")])
        rows = self.jobs.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_key"], first["job_key"])

    def test_the_identity_matches_the_crawlers_own_rule(self) -> None:
        """Storage must not invent a second notion of identity."""
        from crawler.identity import job_identity

        self.jobs.record_many([self.posting()])
        # The same call crawler.observations makes: the company scope comes
        # from name, website and careers URL, not from the company_key.
        expected = job_identity(
            company_name="Acme",
            job_url="https://boards.greenhouse.io/acme/jobs/1",
            job_title="Software Engineer",
            location="Austin, TX",
            platform="Greenhouse",
            job_id="1",
            website="https://acme.com",
            career_url="https://acme.com/careers",
        )
        self.assertEqual(self.jobs.all()[0]["job_key"], expected.job_uid)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
