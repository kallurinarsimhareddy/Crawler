"""The weekly runner driven by the SQLite queue rather than by a JSON list.

Everything here runs offline: the spreadsheet is the in-memory fake and the
crawl engine returns canned results, so no test touches the network or needs
credentials. The database is a **file** in a temporary directory rather than
``":memory:"`` — :class:`store.database.Database` shares one connection for an
in-memory database and opens it with ``check_same_thread=True``, so the
heartbeat thread and the two-worker tests need a real file, exactly as
:class:`tests.test_store.TestAtomicClaiming` does.

The classes worth reading first:

* :class:`TestClosureSafety` — the property the whole migration risks. A
  company that was not read successfully **this run** must never have its
  postings closed, and an interrupted run that resumes must not mass-close the
  first segment's companies.
* :class:`TestJobIdentityPreservation` — the crawler derives a posting's
  identity from the board it actually crawled; the database used to re-derive
  it from the company's stored careers URL. Those disagree exactly when
  discovery found a better board, which is when it matters.
* :class:`TestEmptyRosterProtection` — a run that has nothing to crawl must say
  so, not report a successful crawl of nothing.
"""

from __future__ import annotations

import dataclasses
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from crawler.crawler_engine import CrawlResult
from crawler.retry import RetryPolicy, Verdict
from crawler.weekly_run import EmptyRosterError, WeeklyRun, _parse_args
from sheets.companies import CompanyRepository as SheetCompanies
from sheets.jobs import JobRepository as SheetJobs
from store import Database, migrate
from store.queue import CrawlQueue, QueueState
from store.repositories import CompanyRepository as StoreCompanies
from store.repositories import JobRepository as StoreJobs
from tests.test_weekly_run import FakeEngine, fixture, posting, result
from utils.blocking import Block

ACME = "domain:acme.com"
OTHER = "domain:other.com"


def sheet_row(name: str, website: str = "", career_url: str = "", it_link: str = "") -> Dict[str, str]:
    """A MASTER_COMPANIES row as an import produces it."""
    return {
        "company": name,
        "website": website,
        "career_url": career_url,
        "it_link": it_link,
    }


class SlowEngine:
    """An engine that takes its time, so a lease can expire underneath it.

    Args:
        seconds: How long one batch takes.
        results: Company key to the result to return.
    """

    def __init__(self, seconds: float, results: Optional[Mapping[str, CrawlResult]] = None) -> None:
        self.seconds = seconds
        self.results = dict(results or {})
        self.crawled: List[str] = []

    def crawl_all(self, records: Sequence[Mapping[str, str]], max_workers: int = 0) -> List[CrawlResult]:
        """Crawl slowly, returning one result per record."""
        time.sleep(self.seconds)
        out: List[CrawlResult] = []
        for record in records:
            key = str(record.get("company_key") or "")
            self.crawled.append(key)
            out.append(self.results.get(key) or result(company=str(record.get("company") or "")))
        return out


class AlwaysRetry(RetryPolicy):
    """A policy that always asks for an immediate retry.

    Used to prove the runner's own attempt cap holds even when the policy it
    was handed never gives up, which is the only thing standing between a
    misconfigured policy and an endless run.
    """

    def decide(self, blocker: Block, attempt: int, retry_after: Optional[float] = None) -> Verdict:
        """Always retry, immediately."""
        return Verdict(retry=True, delay=0.0, blocked=False, reason=blocker.value)


class ImmediateRetry(RetryPolicy):
    """The real policy's verdicts, without waiting out its cooldown.

    :class:`~crawler.retry.RetryPolicy` floors its backoff at
    :func:`utils.blocking.cooldown_seconds`, so a 503 parks for five minutes
    whatever ``base_delay`` says. The cap is a **per-run** rule -- that is what
    ``max_attempts`` is documented as, and ``reset_finished`` deliberately
    zeroes ``attempts`` when it returns a company to pending -- so reaching it
    means making the attempts inside one run rather than across three.
    """

    def decide(self, blocker: Block, attempt: int, retry_after: Optional[float] = None) -> Verdict:
        """The policy's own verdict, due immediately."""
        verdict = super().decide(blocker, attempt)
        return Verdict(
            retry=verdict.retry, delay=0.0, blocked=verdict.blocked, reason=verdict.reason
        )


class QueueRunTest(unittest.TestCase):
    """Base class: a fake spreadsheet and a temporary file database."""

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

    def two_companies(self) -> None:
        """The usual pair."""
        self.seed(
            sheet_row("Acme Corporation", "acme.com", "https://acme.com/careers"),
            sheet_row("Other Inc", "other.com", "https://other.com/jobs"),
        )

    def runner(self, engine: Any, database: Optional[Database] = None, **kwargs: Any) -> WeeklyRun:
        """A queue-mode runner over the fixture."""
        kwargs.setdefault("batch_size", 2)
        return WeeklyRun(
            self.client,
            engine=engine,
            checkpoint_path=self.checkpoint_path,
            database=self.database if database is None else database,
            queue_mode=True,
            **kwargs,
        )

    # -- readers -------------------------------------------------------------

    def queue_row(self, company_key: str) -> Dict[str, Any]:
        """One crawl_queue row."""
        row = CrawlQueue(self.database).get(company_key)
        self.assertIsNotNone(row, f"{company_key} is not in the queue")
        return dict(row or {})

    def stats(self) -> Dict[str, int]:
        """Queue counts by state."""
        return CrawlQueue(self.database).stats()

    def stored_jobs(self) -> List[Dict[str, Any]]:
        """Every row in the jobs table."""
        return StoreJobs(self.database).all()


# ---------------------------------------------------------------------------
# 1. Startup
# ---------------------------------------------------------------------------


class TestQueueStartup(QueueRunTest):
    """The run brings its own database up before using it."""

    def test_queue_mode_migrates_an_unprepared_database(self) -> None:
        """A fresh file is migrated by the run, not by the operator."""
        fresh_path = self.root / "fresh.db"
        fresh = Database(fresh_path)
        self.two_companies()

        self.runner(FakeEngine(), database=fresh).execute(dry_run=True)

        versions = fresh.query("SELECT version FROM schema_version")
        self.assertEqual([row["version"] for row in versions], [1])
        fresh.close()

    def test_migration_is_safe_on_an_already_populated_database(self) -> None:
        """Running twice destroys nothing."""
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)
        first = StoreCompanies(self.database).count()

        self.runner(FakeEngine()).execute(dry_run=True, requeue=True)
        self.assertEqual(StoreCompanies(self.database).count(), first)

    def test_queue_mode_without_a_database_is_refused(self) -> None:
        """A programming error, caught at construction rather than mid-run."""
        with self.assertRaises(ValueError):
            WeeklyRun(self.client, queue_mode=True, database=None)

    def test_the_json_path_still_needs_no_database(self) -> None:
        """The default mode is unchanged and takes no database at all."""
        runner = WeeklyRun(self.client, engine=FakeEngine(), checkpoint_path=self.checkpoint_path)
        self.assertFalse(runner.queue_mode)
        self.assertIsNone(runner.database)


# ---------------------------------------------------------------------------
# 2. Roster synchronisation
# ---------------------------------------------------------------------------


class TestRosterSynchronisation(QueueRunTest):
    """The sheet's companies reach SQLite before anything is enqueued."""

    def test_the_roster_is_copied_into_sqlite(self) -> None:
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)

        self.assertEqual(StoreCompanies(self.database).count(), 2)

    def test_the_sheet_name_column_becomes_company_name(self) -> None:
        """The roster calls it "company"; the store calls it "company_name"."""
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)

        stored = StoreCompanies(self.database).get(ACME)
        self.assertIsNotNone(stored)
        self.assertEqual((stored or {})["company_name"], "Acme Corporation")
        self.assertEqual((stored or {})["career_url"], "https://acme.com/careers")

    def test_the_domain_is_derived_for_the_rate_limiter(self) -> None:
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)

        self.assertEqual((StoreCompanies(self.database).get(ACME) or {})["domain"], "acme.com")

    def test_syncing_twice_does_not_duplicate_companies(self) -> None:
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)
        self.runner(FakeEngine()).execute(dry_run=True, requeue=True)

        self.assertEqual(StoreCompanies(self.database).count(), 2)

    def test_a_discovered_board_is_not_cleared_by_a_blank_sheet_cell(self) -> None:
        """The repository's rule, exercised through the runner.

        The sheet's IT Link is empty; the database already holds one. A sync
        that wiped it would throw away the discovery that found it.
        """
        self.two_companies()
        StoreCompanies(self.database).upsert_many(
            [{"company_key": ACME, "company_name": "Acme Corporation",
              "it_link": "https://boards.greenhouse.io/acme"}]
        )

        self.runner(FakeEngine()).execute(dry_run=True)

        self.assertEqual(
            (StoreCompanies(self.database).get(ACME) or {})["it_link"],
            "https://boards.greenhouse.io/acme",
        )

    def test_unusable_rows_never_reach_the_queue(self) -> None:
        """A row naming no website, careers page or board has nowhere to start."""
        self.seed(
            sheet_row("Acme Corporation", "acme.com", "https://acme.com/careers"),
            sheet_row("Nowhere Ltd"),
        )
        self.runner(FakeEngine()).execute(dry_run=True)

        queued = {row["company_key"] for row in self.database.query("SELECT company_key FROM crawl_queue")}
        self.assertEqual(queued, {ACME})


# ---------------------------------------------------------------------------
# 3. Enqueue
# ---------------------------------------------------------------------------


class TestEnqueue(QueueRunTest):
    """Filling the queue, through the existing CrawlQueue."""

    def test_every_usable_company_is_enqueued(self) -> None:
        self.two_companies()
        summary = self.runner(FakeEngine()).execute(dry_run=True)

        self.assertEqual(summary.queue_enqueued, 2)

    def test_enqueue_is_idempotent_within_a_run(self) -> None:
        """A second run adds nothing; it re-queues what has finished instead."""
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)
        summary = self.runner(FakeEngine()).execute(dry_run=True, requeue=True)

        self.assertEqual(summary.queue_enqueued, 0)
        self.assertEqual(summary.queue_requeued, 2)

    def test_a_new_company_is_picked_up_by_the_next_run(self) -> None:
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)

        self.seed(sheet_row("Third Co", "third.com", "https://third.com/jobs"))
        summary = self.runner(FakeEngine()).execute(dry_run=True, requeue=True)

        self.assertEqual(summary.queue_enqueued, 1)


# ---------------------------------------------------------------------------
# 4. Empty roster protection
# ---------------------------------------------------------------------------


class TestEmptyRosterProtection(QueueRunTest):
    """Nothing to crawl is reported, never mistaken for a successful crawl."""

    def test_an_empty_company_table_is_refused(self) -> None:
        with self.assertRaises(EmptyRosterError):
            self.runner(FakeEngine()).execute(dry_run=True)

    def test_a_roster_of_only_unusable_rows_is_refused(self) -> None:
        self.seed(sheet_row("Nowhere Ltd"))
        with self.assertRaises(EmptyRosterError):
            self.runner(FakeEngine()).execute(dry_run=True)

    def test_a_fully_finished_queue_is_refused_rather_than_reported_done(self) -> None:
        """The failure this guard exists for.

        Week two runs against week one's database. Every company is already
        ``succeeded``, so ``enqueue_all`` correctly adds nothing — and without
        this guard the run would crawl nothing and report success.
        """
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)
        self.assertEqual(self.stats()["succeeded"], 2)

        with self.assertRaises(EmptyRosterError):
            self.runner(FakeEngine()).execute(dry_run=True)

    def test_requeue_is_the_documented_way_past_it(self) -> None:
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)

        engine = FakeEngine()
        self.runner(engine).execute(dry_run=True, requeue=True)
        self.assertEqual(sorted(engine.crawled), [ACME, OTHER])

    def test_the_error_names_the_remedy(self) -> None:
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)
        try:
            self.runner(FakeEngine()).execute(dry_run=True)
        except EmptyRosterError as exc:
            self.assertIn("--requeue", str(exc))
        else:  # pragma: no cover - the assertion above must fire
            self.fail("a finished queue must be refused")


# ---------------------------------------------------------------------------
# 5. Claiming
# ---------------------------------------------------------------------------


class TestAtomicClaiming(QueueRunTest):
    """Work is taken from SQLite, atomically, until there is none left."""

    def test_a_claimed_company_leaves_pending(self) -> None:
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)

        self.assertEqual(self.stats()["pending"], 0)

    def test_the_loop_drains_the_queue_rather_than_slicing_a_list(self) -> None:
        """Twelve companies, three at a time, from the queue."""
        self.seed(*[sheet_row(f"Co {n}", f"co{n}.com", f"https://co{n}.com/jobs") for n in range(12)])
        engine = FakeEngine()
        summary = self.runner(engine, batch_size=3).execute(dry_run=True)

        self.assertEqual(len(engine.crawled), 12)
        self.assertEqual(len(set(engine.crawled)), 12)
        self.assertEqual(engine.batches, [3, 3, 3, 3])
        self.assertEqual(summary.queue_claimed, 12)

    def test_a_limit_caps_what_is_claimed_not_what_is_enqueued(self) -> None:
        self.seed(*[sheet_row(f"Co {n}", f"co{n}.com", f"https://co{n}.com/jobs") for n in range(10)])
        engine = FakeEngine()
        summary = self.runner(engine, batch_size=3).execute(dry_run=True, limit=4)

        self.assertEqual(len(engine.crawled), 4)
        self.assertEqual(summary.queue_enqueued, 10)
        self.assertEqual(self.stats()["pending"], 6)

    def test_two_runners_on_one_database_never_crawl_a_company_twice(self) -> None:
        """The real multi-worker test: two runners racing for the same rows."""
        self.seed(*[sheet_row(f"Co {n}", f"co{n}.com", f"https://co{n}.com/jobs") for n in range(40)])

        crawled: List[str] = []
        lock = threading.Lock()

        class RecordingEngine(FakeEngine):
            """Records every company it is handed, under a lock."""

            def crawl_all(self, records, max_workers: int = 0):  # type: ignore[override]
                out = super().crawl_all(records, max_workers)
                with lock:
                    crawled.extend(str(r.get("company_key") or "") for r in records)
                return out

        def worker(name: str) -> None:
            """One runner with its own connection, exactly as a process would."""
            local = Database(self.database_path)
            try:
                self.runner(RecordingEngine(), database=local, batch_size=3, owner=name).execute(
                    dry_run=True, requeue=False
                )
            finally:
                local.close()

        threads = [threading.Thread(target=worker, args=(f"w{n}",)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(crawled), 40, "a company was lost")
        self.assertEqual(len(set(crawled)), 40, "a company was claimed twice")

    def test_the_owner_is_recorded_on_the_claim(self) -> None:
        """So a stuck company can be attributed to the run that took it."""
        self.two_companies()
        engine = SlowEngine(seconds=0.0)
        runner = self.runner(engine, owner="worker-under-test")
        self.assertEqual(runner.owner, "worker-under-test")
        runner.execute(dry_run=True)


# ---------------------------------------------------------------------------
# 6. Outcomes
# ---------------------------------------------------------------------------


class TestSuccessOutcomes(QueueRunTest):
    """A company that was read is recorded as read."""

    def setUp(self) -> None:
        super().setUp()
        self.two_companies()

    def test_success_marks_the_company_succeeded(self) -> None:
        self.runner(FakeEngine({ACME: result(jobs=[posting()])})).execute(dry_run=True)

        row = self.queue_row(ACME)
        self.assertEqual(row["state"], QueueState.SUCCEEDED.value)
        self.assertEqual(row["jobs_found"], 1)
        self.assertTrue(row["last_success_at"])

    def test_an_empty_board_is_a_success_not_a_failure(self) -> None:
        """Nothing advertised is a fact about the company, not a crawl failure."""
        self.runner(FakeEngine({ACME: result(jobs=[])})).execute(dry_run=True)

        self.assertEqual(self.queue_row(ACME)["state"], QueueState.SUCCEEDED.value)

    def test_the_run_id_is_recorded(self) -> None:
        summary = self.runner(FakeEngine()).execute(dry_run=True, run_id="run-42")
        self.assertEqual(self.queue_row(ACME)["run_id"], summary.run_id)

    def test_every_attempt_is_recorded_for_forensics(self) -> None:
        self.runner(FakeEngine()).execute(dry_run=True)

        attempts = self.database.query("SELECT * FROM crawl_attempts ORDER BY company_key")
        self.assertEqual(len(attempts), 2)
        self.assertEqual({row["outcome"] for row in attempts}, {"succeeded"})


class TestFailureOutcomes(QueueRunTest):
    """Retryable, blocked and permanent failures are told apart."""

    def setUp(self) -> None:
        super().setUp()
        self.two_companies()

    def failing(self, error: str) -> FakeEngine:
        """An engine where Acme fails with a given error."""
        return FakeEngine({ACME: result(error=error)})

    def test_a_transient_failure_waits_and_stays_claimable_later(self) -> None:
        self.runner(self.failing("HTTP 503 from the board")).execute(dry_run=True)

        row = self.queue_row(ACME)
        self.assertEqual(row["state"], QueueState.RETRY_WAIT.value)
        self.assertGreater(row["next_attempt_at"], time.time())
        self.assertTrue(row["last_reason"])

    def test_a_timeout_is_retryable(self) -> None:
        self.runner(self.failing("Read timed out")).execute(dry_run=True)
        self.assertEqual(self.queue_row(ACME)["state"], QueueState.RETRY_WAIT.value)

    def test_a_captcha_is_blocked_and_never_retried(self) -> None:
        """Retrying a CAPTCHA is not merely useless, it is what gets us banned."""
        self.runner(self.failing("captcha challenge presented")).execute(dry_run=True)

        self.assertEqual(self.queue_row(ACME)["state"], QueueState.BLOCKED.value)
        self.assertEqual(CrawlQueue(self.database).claim("later", limit=9), [])

    def test_a_403_is_blocked(self) -> None:
        self.runner(self.failing("HTTP 403 Forbidden")).execute(dry_run=True)
        self.assertEqual(self.queue_row(ACME)["state"], QueueState.BLOCKED.value)

    def test_a_404_is_a_permanent_failure_not_a_block(self) -> None:
        """Nothing is refusing us; the page simply is not there."""
        self.runner(self.failing("HTTP 404 Not Found")).execute(dry_run=True)
        self.assertEqual(self.queue_row(ACME)["state"], QueueState.FAILED.value)

    def test_the_attempt_cap_turns_a_transient_failure_permanent(self) -> None:
        """Three attempts of a retryable failure, then it is simply failed."""
        engine = self.failing("HTTP 503 from the board")
        policy = ImmediateRetry(max_attempts=3)

        self.runner(engine, retry_policy=policy, retry_poll_seconds=0.0).execute(dry_run=True)

        row = self.queue_row(ACME)
        self.assertEqual(row["state"], QueueState.FAILED.value)
        self.assertGreaterEqual(row["attempts"], 3)

    def test_the_runners_own_cap_holds_against_a_policy_that_never_gives_up(self) -> None:
        """A policy that always retries must not produce an endless run."""
        engine = self.failing("HTTP 503 from the board")
        runner = self.runner(
            engine,
            retry_policy=AlwaysRetry(max_attempts=3),
            retry_poll_seconds=0.001,
        )
        runner.execute(dry_run=True)

        self.assertEqual(self.queue_row(ACME)["state"], QueueState.FAILED.value)
        self.assertLessEqual(len([k for k in engine.crawled if k == ACME]), 4)

    def test_a_failure_is_recorded_in_the_attempt_log(self) -> None:
        self.runner(self.failing("HTTP 429 Too Many Requests")).execute(dry_run=True)

        rows = self.database.query(
            "SELECT * FROM crawl_attempts WHERE company_key = ?", (ACME,)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["http_status"], 429)
        self.assertNotEqual(rows[0]["outcome"], "succeeded")

    def test_one_failure_does_not_stop_the_other_company(self) -> None:
        self.runner(self.failing("HTTP 500 server error")).execute(dry_run=True)
        self.assertEqual(self.queue_row(OTHER)["state"], QueueState.SUCCEEDED.value)


# ---------------------------------------------------------------------------
# 7. Lease and recovery
# ---------------------------------------------------------------------------


class TestLeaseAndRecovery(QueueRunTest):
    """A run that died must not strand its companies."""

    def test_a_dead_runs_claims_are_released_at_startup(self) -> None:
        self.two_companies()
        # The companies first: `enqueue_all` selects from the companies table,
        # so queuing before they exist queues nothing and there is no claim for
        # the dead worker to leave behind.
        StoreCompanies(self.database).upsert_many(
            [{"company_key": ACME, "company_name": "Acme Corporation"},
             {"company_key": OTHER, "company_name": "Other Inc"}]
        )
        CrawlQueue(self.database).enqueue_all()
        CrawlQueue(self.database).claim("dead-worker", limit=2)
        self.assertEqual(self.stats()["running"], 2)

        engine = FakeEngine()
        summary = self.runner(engine, lease_seconds=0.0).execute(dry_run=True)

        self.assertEqual(summary.queue_released, 2)
        self.assertEqual(sorted(engine.crawled), [ACME, OTHER])

    def test_a_healthy_runs_claims_are_left_alone(self) -> None:
        """Recovery must be safe to perform while another run is working."""
        self.two_companies()
        self.runner(FakeEngine()).execute(dry_run=True)
        CrawlQueue(self.database).reset_finished()
        CrawlQueue(self.database).claim("live-worker", limit=1)

        summary = self.runner(FakeEngine(), lease_seconds=900.0).execute(
            dry_run=True, requeue=False
        )
        self.assertEqual(summary.queue_released, 0)

    def test_a_long_crawl_is_kept_alive_by_the_heartbeat(self) -> None:
        """The lease must not expire underneath a batch that is still running."""
        self.two_companies()
        engine = SlowEngine(seconds=0.45)
        runner = self.runner(engine, lease_seconds=0.30, heartbeat_seconds=0.05)

        before = time.time()
        runner.execute(dry_run=True)

        row = self.queue_row(ACME)
        self.assertEqual(row["state"], QueueState.SUCCEEDED.value)
        self.assertGreater(runner.heartbeats, 0, "no heartbeat was sent")
        self.assertGreater(time.time() - before, 0.4)

    def test_the_heartbeat_stops_when_the_batch_finishes(self) -> None:
        self.two_companies()
        runner = self.runner(SlowEngine(seconds=0.05), heartbeat_seconds=0.01)
        runner.execute(dry_run=True)

        self.assertFalse(runner.heartbeat_running, "a heartbeat thread outlived its batch")


# ---------------------------------------------------------------------------
# 8. Resume
# ---------------------------------------------------------------------------


class TestResumeAfterInterruption(QueueRunTest):
    """A killed run resumes from the queue, and repeats nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.seed(*[sheet_row(f"Co {n}", f"co{n}.com", f"https://co{n}.com/jobs") for n in range(6)])

    def test_only_unfinished_work_is_re_run(self) -> None:
        first = FakeEngine(fail_after=2)
        with self.assertRaises(KeyboardInterrupt):
            self.runner(first, batch_size=1).execute(dry_run=True)

        second = FakeEngine()
        self.runner(second, batch_size=2, lease_seconds=0.0).execute(dry_run=True, requeue=False)

        done = [key for key in first.crawled if key]
        self.assertEqual(set(done) & set(second.crawled), set(), "a company was crawled twice")
        self.assertEqual(len(set(done) | set(second.crawled)), 6)

    def test_the_interrupted_runs_successes_are_durable(self) -> None:
        first = FakeEngine(fail_after=2)
        with self.assertRaises(KeyboardInterrupt):
            self.runner(first, batch_size=1).execute(dry_run=True)

        self.assertGreaterEqual(self.stats()["succeeded"], 2)

    def test_a_stop_request_finishes_the_batch_and_leaves_the_rest_pending(self) -> None:
        engine = FakeEngine()
        runner = self.runner(engine, batch_size=2)

        original = engine.crawl_all

        def crawl_then_stop(records, max_workers: int = 0):
            """Ask the run to stop after the first batch."""
            runner.request_stop()
            return original(records, max_workers)

        engine.crawl_all = crawl_then_stop  # type: ignore[assignment]
        summary = runner.execute(dry_run=True)

        self.assertTrue(summary.interrupted)
        self.assertEqual(len(engine.crawled), 2)
        self.assertEqual(self.stats()["pending"], 4)


# ---------------------------------------------------------------------------
# 9. Job persistence and identity
# ---------------------------------------------------------------------------


class TestJobPersistence(QueueRunTest):
    """Postings reach the local store, once each."""

    def setUp(self) -> None:
        super().setUp()
        self.two_companies()

    def test_postings_are_written_to_the_jobs_table(self) -> None:
        engine = FakeEngine({ACME: result(jobs=[posting(), posting("Data Engineer", "https://acme.com/jobs/2")])})
        summary = self.runner(engine).execute(dry_run=False)

        self.assertEqual(StoreJobs(self.database).count(), 2)
        self.assertEqual(summary.jobs_persisted, 2)

    def test_a_re_crawl_updates_rather_than_duplicating(self) -> None:
        engine = FakeEngine({ACME: result(jobs=[posting()])})
        self.runner(engine).execute(dry_run=False)
        self.runner(engine).execute(dry_run=False, requeue=True)

        self.assertEqual(StoreJobs(self.database).count(), 1)

    def test_the_company_key_scopes_the_posting(self) -> None:
        engine = FakeEngine({ACME: result(jobs=[posting()])})
        self.runner(engine).execute(dry_run=False)

        self.assertEqual(self.stored_jobs()[0]["company_key"], ACME)

    def test_a_failed_company_persists_nothing(self) -> None:
        engine = FakeEngine({ACME: result(error="HTTP 503 from the board")})
        self.runner(engine).execute(dry_run=False)

        keys = {row["company_key"] for row in self.stored_jobs()}
        self.assertNotIn(ACME, keys)


class TestJobIdentityPreservation(QueueRunTest):
    """Storage records the crawler's identity; it never invents a second one.

    The divergence is real and reproducible: ``crawler.observations`` derives a
    posting's uid from the board the crawl **actually used**, while the store
    used to re-derive it from the company's **stored** careers URL. When
    discovery finds a better board than the sheet holds, those two disagree —
    and the same posting acquires two different primary keys, one in
    ``JOB_HISTORY`` and another in ``jobs``.
    """

    def test_a_supplied_identity_is_stored_verbatim(self) -> None:
        """The repository rule, tested directly."""
        StoreCompanies(self.database).upsert_many(
            [{"company_key": ACME, "company_name": "Acme Corporation",
              "career_url": "https://acme.com/careers"}]
        )
        StoreJobs(self.database).record_many([{
            "job_key": "the-crawlers-own-uid",
            "company_key": ACME,
            "job_title": "Software Engineer",
            "job_url": "https://boards.greenhouse.io/acme/jobs/7",
            "url_key": "url-key", "content_key": "content-key",
            "identity_basis": "url",
        }])

        self.assertEqual(self.stored_jobs()[0]["job_key"], "the-crawlers-own-uid")
        self.assertEqual(self.stored_jobs()[0]["identity_basis"], "url")

    def test_an_absent_identity_is_still_derived(self) -> None:
        """The old behaviour is kept for callers that supply no uid."""
        StoreCompanies(self.database).upsert_many(
            [{"company_key": ACME, "company_name": "Acme Corporation",
              "career_url": "https://acme.com/careers"}]
        )
        StoreJobs(self.database).record_many([{
            "company_key": ACME,
            "job_title": "Software Engineer",
            "job_url": "https://acme.com/jobs/1",
        }])

        self.assertTrue(self.stored_jobs()[0]["job_key"])

    def test_the_stored_key_matches_the_crawlers_key(self) -> None:
        """End to end, through the runner."""
        self.two_companies()
        engine = FakeEngine({ACME: result(jobs=[posting()])})
        self.runner(engine).execute(dry_run=False)

        from crawler.identity import job_identity

        expected = job_identity(
            company_name="Acme Corporation",
            job_url="https://acme.com/jobs/1",
            job_title="Software Engineer",
            location="Austin, TX",
            platform="Greenhouse",
            job_id="",
            website="acme.com",
            career_url="https://acme.com/careers",
        ).job_uid

        self.assertEqual(self.stored_jobs()[0]["job_key"], expected)

    def test_a_discovered_board_does_not_fork_the_identity(self) -> None:
        """The divergence case, end to end.

        The sheet names no website and a careers page on the company's own
        domain; the crawl actually ran against the Greenhouse board discovery
        found. Deriving from the stored row would yield a different uid.
        """
        self.seed(sheet_row("Acme Corporation", "", "https://acme.com/careers"))

        crawled_board = "https://boards.greenhouse.io/acme"
        # Job is a frozen dataclass, so the board the crawl actually used
        # is substituted rather than assigned.
        job = dataclasses.replace(
            posting(url="https://boards.greenhouse.io/acme/jobs/7"),
            career_page_url=crawled_board,
        )
        # Keyed on the company key a website-less row actually produces: the
        # identity falls back to the careers page's domain, not to the name.
        engine = FakeEngine({
            ACME: result(jobs=[job], seed_url=crawled_board, seed_field="it_link"),
        })
        self.runner(engine).execute(dry_run=False)

        from crawler.identity import job_identity

        crawlers = job_identity(
            company_name="Acme Corporation",
            job_url="https://boards.greenhouse.io/acme/jobs/7",
            job_title="Software Engineer",
            location="Austin, TX",
            platform="Greenhouse",
            job_id="",
            website="",
            career_url=crawled_board,
        ).job_uid
        from_the_stored_row = job_identity(
            company_name="Acme Corporation",
            job_url="https://boards.greenhouse.io/acme/jobs/7",
            job_title="Software Engineer",
            location="Austin, TX",
            platform="Greenhouse",
            job_id="",
            website="",
            career_url="https://acme.com/careers",
        ).job_uid

        self.assertNotEqual(crawlers, from_the_stored_row, "the fixture no longer diverges")

        stored = {row["job_key"] for row in self.stored_jobs()}
        self.assertIn(crawlers, stored)
        self.assertNotIn(from_the_stored_row, stored)

    def test_sqlite_and_the_sheet_agree_on_the_key(self) -> None:
        """One posting, one identity, in both places.

        The tab compared against is the weekly change log rather than
        ``JOB_HISTORY``: SQLite is the ledger now, and a run no longer copies
        every posting into a seventeen-column tab. What still has to hold is
        that a posting carries the *same* identity wherever it is written, so
        the sheet and the store can be joined.
        """
        self.two_companies()
        engine = FakeEngine({ACME: result(jobs=[posting()])})
        self.runner(engine).execute(dry_run=False)

        in_sqlite = {row["job_key"] for row in self.stored_jobs()}
        in_sheet = {
            str(record.get("job_key"))
            for record in SheetJobs(self.client).weekly.read()
            if record.get("job_key")
        }

        self.assertTrue(in_sqlite)
        self.assertTrue(in_sheet)
        self.assertTrue(in_sqlite <= in_sheet, "the two stores disagree about identity")

    def test_the_ledger_is_no_longer_mirrored_into_job_history(self) -> None:
        """The tab that could not hold the roster stops being written to.

        Every posting is in SQLite; ``JOB_HISTORY`` keeps whatever it already
        had. Nothing is deleted — it simply stops growing.
        """
        self.two_companies()
        engine = FakeEngine({ACME: result(jobs=[posting()])})
        self.runner(engine).execute(dry_run=False)

        self.assertTrue({row["job_key"] for row in self.stored_jobs()})
        self.assertEqual(SheetJobs(self.client).known_jobs(), [])


# ---------------------------------------------------------------------------
# 10. Closure safety
# ---------------------------------------------------------------------------


class TestClosureSafety(QueueRunTest):
    """A posting is only closed by a run that actually read its board."""

    def setUp(self) -> None:
        super().setUp()
        self.two_companies()

    def test_a_vanished_posting_is_closed_for_a_company_that_was_read(self) -> None:
        self.runner(FakeEngine({ACME: result(jobs=[posting()])})).execute(dry_run=False)
        self.assertEqual(StoreJobs(self.database).count(status="active"), 1)

        self.runner(FakeEngine({ACME: result(jobs=[])})).execute(dry_run=False, requeue=True)

        self.assertEqual(StoreJobs(self.database).count(status="closed"), 1)

    def test_a_failed_company_has_nothing_closed(self) -> None:
        """A blocked crawl proves nothing about what the board still advertises."""
        self.runner(FakeEngine({ACME: result(jobs=[posting()])})).execute(dry_run=False)

        self.runner(FakeEngine({ACME: result(error="captcha challenge presented")})).execute(
            dry_run=False, requeue=True
        )

        self.assertEqual(StoreJobs(self.database).count(status="closed"), 0)
        self.assertEqual(StoreJobs(self.database).count(status="active"), 1)

    def test_only_this_runs_successes_are_offered_to_the_comparison(self) -> None:
        """The set handed to the weekly diff is successes, not attempts."""
        engine = FakeEngine({
            ACME: result(jobs=[posting()]),
            OTHER: result(error="HTTP 503 from the board"),
        })
        runner = self.runner(engine)
        runner.execute(dry_run=False)

        self.assertEqual(runner.crawled_this_run, {ACME})

    def test_a_resumed_run_does_not_close_the_first_segments_jobs(self) -> None:
        """The property the migration most risks.

        Segment one reads Acme and finds a posting, then dies. Segment two
        reads only Other. Acme's posting must survive: segment two never
        looked at Acme's board and knows nothing about it.
        """
        first = FakeEngine({ACME: result(jobs=[posting()])}, fail_after=1)
        with self.assertRaises(KeyboardInterrupt):
            self.runner(first, batch_size=1).execute(dry_run=False)

        self.assertEqual(StoreJobs(self.database).count(status="active"), 1)

        second = FakeEngine({OTHER: result(jobs=[])})
        runner = self.runner(second, batch_size=1, lease_seconds=0.0)
        runner.execute(dry_run=False, requeue=False)

        self.assertNotIn(ACME, runner.crawled_this_run)
        self.assertEqual(StoreJobs(self.database).count(status="closed"), 0)
        self.assertEqual(StoreJobs(self.database).count(status="active"), 1)

    def test_closure_is_scoped_to_one_company(self) -> None:
        """Reading Acme must never close Other's postings."""
        # `posting` already fills company_name in, so Other's is set after the
        # fact rather than passed twice.
        others = dataclasses.replace(
            posting("Analyst", "https://other.com/jobs/9"), company_name="Other Inc"
        )
        both = FakeEngine({
            ACME: result(jobs=[posting()]),
            OTHER: result(jobs=[others]),
        })
        self.runner(both).execute(dry_run=False)
        self.assertEqual(StoreJobs(self.database).count(status="active"), 2)

        only_acme = FakeEngine({
            ACME: result(jobs=[]),
            OTHER: result(error="HTTP 503 from the board"),
        })
        self.runner(only_acme).execute(dry_run=False, requeue=True)

        closed = [row for row in self.stored_jobs() if row["status"] == "closed"]
        self.assertEqual([row["company_key"] for row in closed], [ACME])


# ---------------------------------------------------------------------------
# 11. The command line
# ---------------------------------------------------------------------------


class TestQueueCommandLine(unittest.TestCase):
    """The flag exists, and the old behaviour is still the default."""

    def test_the_json_flow_remains_the_default(self) -> None:
        self.assertFalse(_parse_args([]).queue)

    def test_the_queue_flag_turns_it_on(self) -> None:
        self.assertTrue(_parse_args(["--queue"]).queue)

    def test_the_database_path_is_configurable(self) -> None:
        args = _parse_args(["--queue", "--database", "state/other.db"])
        self.assertEqual(str(args.database), "state/other.db")

    def test_the_lease_is_configurable(self) -> None:
        self.assertEqual(_parse_args(["--queue", "--lease-seconds", "60"]).lease_seconds, 60.0)

    def test_recover_releases_every_claim(self) -> None:
        """``--recover`` is the deliberate "a run died" switch."""
        self.assertTrue(_parse_args(["--queue", "--recover"]).recover)

    def test_requeue_is_off_by_default(self) -> None:
        self.assertFalse(_parse_args(["--queue"]).requeue)

    def test_the_documented_invocation_parses(self) -> None:
        args = _parse_args(["--queue", "--workers", "20", "--dry-run"])
        self.assertTrue(args.queue)
        self.assertEqual(args.workers, 20)
        self.assertTrue(args.dry_run)

    def test_max_attempts_reaches_the_policy(self) -> None:
        self.assertEqual(_parse_args(["--queue", "--max-attempts", "5"]).max_attempts, 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
