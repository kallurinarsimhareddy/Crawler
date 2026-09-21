"""Worker lifecycle: delivery, idempotency, bounded retries, crash recovery,
heartbeat expiry, cancellation, orphans and shutdown.

The fast tests use the in-memory repository and queue with controllable clocks,
so "ten minutes later" takes no time. ``TestWorkerOnPostgresAndRedis`` repeats
the essential scenarios on real PostgreSQL and the Redis queue scripts.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cloud.shared.models import JobStatus, JobType, ResultKind
from cloud.shared.queue import InMemoryJobQueue, RedisJobQueue
from cloud.shared.repository import InMemoryJobRepository
from cloud.shared.schemas import parse_job_request
from cloud.shared.service import JobService, RetryPolicy
from cloud.shared.storage import LocalFileStorage
from cloud.tests._pg import PostgresTestCase
from cloud.worker.executor import JobExecutor
from cloud.worker.fake_runner import FakeRunner
from cloud.worker.results import ResultWriter
from cloud.worker.runner import JobRunner, RunResult
from cloud.worker.worker import Worker, WorkerConfig

OWNER = str(uuid.uuid4())


class Clocks:
    """One notion of 'now' for the repository (datetime) and the queue (epoch seconds)."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

    def dt(self) -> datetime:
        return self.now

    def epoch(self) -> float:
        return self.now.timestamp()

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class CountingRunner(FakeRunner):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.calls = 0

    def run(self, job, context):
        self.calls += 1
        return super().run(job, context)


class FlakyRunner(JobRunner):
    """Raises for the first ``failures`` runs, then completes."""

    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def run(self, job, context):
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError(f"board unreachable (call {self.calls})")
        context.report_progress(1, 1, "done")
        return RunResult.completed()


class LoopingRunner(JobRunner):
    """Runs until told to stop; records how it ended."""

    name = "looping"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.saw_cancel = threading.Event()

    def run(self, job, context):
        self.started.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if context.is_cancelled():
                self.saw_cancel.set()
                return RunResult.cancelled()
            time.sleep(0.01)
        return RunResult.failed("never cancelled")


class WorkerHarness(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.clocks = Clocks()
        self.repo = InMemoryJobRepository(clock=self.clocks.dt)
        self.service = JobService(self.repo, clock=self.clocks.dt)
        self.queue = InMemoryJobQueue(clock=self.clocks.epoch)
        self.storage = LocalFileStorage(self.scratch / "results")
        self.policy = RetryPolicy(max_attempts=3, base_delay_seconds=10, max_delay_seconds=100)

    def worker(self, runner: JobRunner, *, worker_id: str = "worker-1", stopping=None, heartbeat=1000.0, lease=60.0) -> Worker:
        stopping = stopping or threading.Event()
        executor = JobExecutor(
            self.service,
            runner,
            worker_id=worker_id,
            lease_seconds=lease,
            heartbeat_interval=heartbeat,
            retry_policy=self.policy,
            result_writer=ResultWriter(self.storage),
            runtime_root=self.scratch / "runtime",
            on_requeue=lambda job_id, delay: self.queue.enqueue(job_id, delay_seconds=delay),
            stopping=stopping,
            cancel_check_interval=0,
        )
        return Worker(
            self.service, self.queue, executor,
            config=WorkerConfig(orphan_after_seconds=600, visibility_timeout=300),
            retry_policy=self.policy, stopping=stopping,
        )

    def submit(self, payload=None, *, enqueue: bool = True, max_attempts: int = 3) -> str:
        request = parse_job_request(payload or {"type": "single_company", "website": "example.com"})
        job = self.service.create_job(request, owner_id=OWNER, max_attempts=max_attempts)
        if enqueue:
            self.queue.enqueue(job.job_id)
        return job.job_id

    def job(self, job_id):
        return self.service.get_job(job_id)

    def events(self, job_id):
        return [event.kind for event in self.service.list_events(job_id)]


class TestHappyPathAndIdempotency(WorkerHarness):
    def test_a_delivered_job_runs_to_completion_with_results(self) -> None:
        runner = CountingRunner()
        job_id = self.submit()
        self.assertTrue(self.worker(runner).process_next())

        job = self.job(job_id)
        self.assertIs(job.status, JobStatus.COMPLETED)
        self.assertEqual((job.attempts, job.progress.completed, job.progress.total), (1, 1, 1))
        self.assertEqual(job.progress.current_phase, "completed")
        self.assertIsNone(job.lease_expires_at)
        kinds = {r.kind for r in self.service.list_results(job_id, owner_id=OWNER)}
        self.assertEqual(kinds, {ResultKind.SUMMARY_JSON, ResultKind.JOBS_CSV})
        self.assertEqual(self.events(job_id), ["created", "claimed", "completed"])
        self.assertEqual(self.queue.stats().in_flight, 0)
        self.assertFalse((self.scratch / "runtime" / job_id).exists(), "workspace should be cleaned up")

    def test_duplicate_deliveries_run_the_job_once(self) -> None:
        runner = CountingRunner()
        job_id = self.submit()
        worker = self.worker(runner)
        worker.process_next()
        for _ in range(3):
            self.queue.enqueue(job_id)
            worker.process_next()
        self.assertEqual(runner.calls, 1)
        self.assertIs(self.job(job_id).status, JobStatus.COMPLETED)
        self.assertEqual(self.queue.stats(), type(self.queue.stats())(0, 0, 0))

    def test_a_job_held_by_another_worker_is_not_run_twice(self) -> None:
        runner = CountingRunner()
        job_id = self.submit()
        self.service.claim_job(job_id, worker_id="someone-else", lease_seconds=60)
        self.worker(runner).process_next()
        self.assertEqual(runner.calls, 0)
        self.assertEqual(self.job(job_id).worker_id, "someone-else")

    def test_unsupported_types_are_left_queued(self) -> None:
        class SingleOnly(CountingRunner):
            supported_types = frozenset({JobType.SINGLE_COMPANY})

        runner = SingleOnly()
        job_id = self.submit({"type": "weekly_crawl"})
        self.worker(runner).process_next()
        self.assertEqual(runner.calls, 0)
        self.assertIs(self.job(job_id).status, JobStatus.QUEUED)
        self.assertEqual(self.job(job_id).attempts, 0)

    def test_an_unknown_job_id_is_acknowledged_and_dropped(self) -> None:
        self.queue.enqueue("job_" + "f" * 32)
        with self.assertLogs("cloud.worker.worker", level="ERROR"):
            self.assertTrue(self.worker(CountingRunner()).process_next())
        self.assertEqual(self.queue.stats().in_flight, 0)


class TestRetries(WorkerHarness):
    def test_retries_are_bounded_and_delayed(self) -> None:
        runner = FlakyRunner(failures=99)
        job_id = self.submit(max_attempts=3)
        worker = self.worker(runner)

        with self.assertLogs("cloud.worker.executor", level="ERROR"):
            worker.process_next()
        job = self.job(job_id)
        self.assertEqual((job.status, job.attempts), (JobStatus.QUEUED, 1))
        self.assertIn("board unreachable", job.error)
        self.assertEqual(self.queue.stats().delayed, 1)
        self.assertFalse(worker.process_next(), "the retry must wait for its delay")

        self.clocks.advance(10)
        with self.assertLogs("cloud.worker.executor", level="ERROR"):
            worker.process_next()
        self.assertEqual(self.job(job_id).attempts, 2)
        self.assertFalse(worker.process_next())

        self.clocks.advance(20)  # the delay doubles
        with self.assertLogs("cloud.worker.executor", level="ERROR"):
            worker.process_next()

        job = self.job(job_id)
        self.assertEqual((job.status, job.attempts), (JobStatus.FAILED, 3))
        self.assertIn("gave up after 3 attempts", job.error)
        self.assertEqual(runner.calls, 3)
        self.clocks.advance(10_000)
        self.assertFalse(worker.process_next(), "nothing is left to retry")
        self.assertEqual(self.events(job_id).count("retry_scheduled"), 2)

    def test_a_retry_that_succeeds_completes(self) -> None:
        runner = FlakyRunner(failures=1)
        job_id = self.submit(max_attempts=3)
        worker = self.worker(runner)
        with self.assertLogs("cloud.worker.executor", level="ERROR"):
            worker.process_next()
        self.clocks.advance(10)
        worker.process_next()
        job = self.job(job_id)
        self.assertEqual((job.status, job.attempts, job.error), (JobStatus.COMPLETED, 2, None))

    def test_a_non_retryable_failure_fails_at_once(self) -> None:
        job_id = self.submit(max_attempts=3)
        self.worker(FakeRunner(fail_with="bad input")).process_next()
        job = self.job(job_id)
        self.assertEqual((job.status, job.attempts, job.error), (JobStatus.FAILED, 1, "bad input"))

    def test_the_policy_caps_whatever_the_job_asks_for(self) -> None:
        self.policy = RetryPolicy(max_attempts=1, base_delay_seconds=0)
        job_id = self.submit(max_attempts=5)
        with self.assertLogs("cloud.worker.executor", level="ERROR"):
            self.worker(FlakyRunner(failures=99)).process_next()
        self.assertIs(self.job(job_id).status, JobStatus.FAILED)

    def test_retry_policy_validation(self) -> None:
        for bad in (dict(max_attempts=0), dict(max_attempts=11), dict(base_delay_seconds=-1)):
            with self.subTest(bad), self.assertRaises(ValueError):
                RetryPolicy(**bad)
        self.assertEqual([RetryPolicy(base_delay_seconds=30, max_delay_seconds=100).delay_after(n) for n in (1, 2, 3, 4)], [30, 60, 100, 100])


class TestCrashRecovery(WorkerHarness):
    def test_a_job_whose_worker_died_is_requeued_and_finished_by_another(self) -> None:
        job_id = self.submit()
        # Worker "dead" takes the delivery, claims the job, and then the process dies:
        dead_delivery = self.queue.reserve("dead", visibility_timeout=300)
        dead_claim = self.service.claim_job(job_id, worker_id="dead", lease_seconds=60)
        self.assertIsNotNone(dead_delivery)

        survivor = self.worker(CountingRunner(), worker_id="survivor")
        self.assertEqual(survivor.maintain().requeued, [], "lease still valid: nothing to reap")

        self.clocks.advance(61)
        with self.assertLogs("cloud.shared.service", level="WARNING"):
            report = survivor.maintain()
        self.assertEqual(report.requeued, [job_id])
        self.assertEqual((self.job(job_id).status, self.job(job_id).attempts), (JobStatus.QUEUED, 1))

        self.clocks.advance(300)  # retry delay and the dead delivery's visibility both pass
        survivor.process_next()
        survivor.process_next()
        job = self.job(job_id)
        self.assertEqual((job.status, job.attempts, job.worker_id), (JobStatus.COMPLETED, 2, "survivor"))

        # The dead worker comes back to life. Nothing it writes lands.
        self.assertIsNone(self.service.update_progress(dead_claim, message="zombie"))
        self.assertIsNone(self.service.finish_failed(dead_claim, "zombie failure"))
        self.assertIsNone(self.service.heartbeat(dead_claim, lease_seconds=60))
        self.assertIs(self.job(job_id).status, JobStatus.COMPLETED)
        self.assertIn("reaped_requeued", self.events(job_id))

    def test_a_dead_job_out_of_attempts_is_failed_not_requeued(self) -> None:
        job_id = self.submit(max_attempts=1, enqueue=False)
        self.service.claim_job(job_id, worker_id="dead", lease_seconds=60)
        self.clocks.advance(61)
        with self.assertLogs("cloud.shared.service", level="WARNING"):
            report = self.worker(CountingRunner()).maintain()
        self.assertEqual(report.failed, [job_id])
        self.assertIn("stopped responding", self.job(job_id).error)
        self.assertEqual(self.queue.stats().delayed + self.queue.stats().ready, 0)

    def test_a_dead_job_with_a_cancel_request_is_cancelled(self) -> None:
        job_id = self.submit()
        self.service.claim_job(job_id, worker_id="dead", lease_seconds=60)
        self.service.request_cancel(job_id, owner_id=OWNER)
        self.clocks.advance(61)
        with self.assertLogs("cloud.shared.service", level="WARNING"):
            report = self.worker(CountingRunner()).maintain()
        self.assertEqual(report.cancelled, [job_id])
        self.assertIs(self.job(job_id).status, JobStatus.CANCELLED)

    def test_two_reapers_do_not_double_requeue(self) -> None:
        job_id = self.submit()
        self.service.claim_job(job_id, worker_id="dead", lease_seconds=60)
        self.clocks.advance(61)
        with self.assertLogs("cloud.shared.service", level="WARNING"):
            first = self.worker(CountingRunner(), worker_id="a").maintain()
        second = self.worker(CountingRunner(), worker_id="b").maintain()
        self.assertEqual((first.requeued, second.requeued), ([job_id], []))


class TestHeartbeats(WorkerHarness):
    def test_heartbeats_extend_the_lease_while_running(self) -> None:
        runner = LoopingRunner()
        job_id = self.submit()
        stopping = threading.Event()
        worker = self.worker(runner, heartbeat=0.02, lease=60, stopping=stopping)
        thread = threading.Thread(target=worker.process_next)
        thread.start()
        self.assertTrue(runner.started.wait(5))
        first = self.job(job_id).lease_expires_at
        self.clocks.advance(30)
        time.sleep(0.2)
        self.assertGreater(self.job(job_id).lease_expires_at, first)
        self.service.request_cancel(job_id, owner_id=OWNER)
        thread.join(10)
        self.assertIs(self.job(job_id).status, JobStatus.CANCELLED)

    def test_a_worker_that_loses_its_lease_stops_and_records_nothing(self) -> None:
        runner = LoopingRunner()
        job_id = self.submit()
        worker = self.worker(runner, heartbeat=0.02, lease=60)
        thread = threading.Thread(target=worker.process_next)
        thread.start()
        self.assertTrue(runner.started.wait(5))

        # The worker stalls long enough for its lease to lapse and the reaper to act.
        self.clocks.advance(61)
        with self.assertLogs("cloud.shared.service", level="WARNING"):
            self.service.reap_stale(self.policy)
        thread.join(10)

        self.assertTrue(runner.saw_cancel.is_set())
        job = self.job(job_id)
        self.assertEqual((job.status, job.attempts), (JobStatus.QUEUED, 1))
        self.assertEqual(self.service.list_results(job_id), [])
        self.assertNotIn("cancelled", self.events(job_id))


class TestCancellation(WorkerHarness):
    def test_a_queued_job_cancelled_before_delivery_never_runs(self) -> None:
        runner = CountingRunner()
        job_id = self.submit()
        self.assertIs(self.service.request_cancel(job_id, owner_id=OWNER).status, JobStatus.CANCELLED)
        self.worker(runner).process_next()
        self.assertEqual(runner.calls, 0)
        self.assertIs(self.job(job_id).status, JobStatus.CANCELLED)

    def test_a_running_job_stops_when_its_owner_cancels(self) -> None:
        runner = LoopingRunner()
        job_id = self.submit()
        thread = threading.Thread(target=self.worker(runner).process_next)
        thread.start()
        self.assertTrue(runner.started.wait(5))

        requested = self.service.request_cancel(job_id, owner_id=OWNER)
        self.assertEqual((requested.status, requested.cancel_requested), (JobStatus.RUNNING, True))
        self.assertIs(self.service.request_cancel(job_id, owner_id=OWNER).status, JobStatus.RUNNING, "asking twice is harmless")
        thread.join(10)

        job = self.job(job_id)
        self.assertIs(job.status, JobStatus.CANCELLED)
        self.assertIsNotNone(job.completed_at)
        self.assertEqual(self.events(job_id)[-2:], ["cancel_requested", "cancelled"])

    def test_another_user_cannot_cancel(self) -> None:
        from cloud.shared.service import JobNotFoundError

        job_id = self.submit()
        with self.assertRaises(JobNotFoundError):
            self.service.request_cancel(job_id, owner_id=str(uuid.uuid4()))


class TestOrphansAndShutdown(WorkerHarness):
    def test_queued_jobs_the_queue_lost_are_enqueued_again_once(self) -> None:
        job_id = self.submit(enqueue=False)
        worker = self.worker(CountingRunner())
        self.assertEqual(worker.maintain().orphans_enqueued, [])
        self.clocks.advance(601)
        self.assertEqual(worker.maintain().orphans_enqueued, [job_id])
        self.assertEqual(worker.maintain().orphans_enqueued, [], "touched: not swept again immediately")
        worker.process_next()
        self.assertIs(self.job(job_id).status, JobStatus.COMPLETED)

    def test_orphan_sweep_skips_types_the_runner_cannot_run(self) -> None:
        class SingleOnly(CountingRunner):
            supported_types = frozenset({JobType.SINGLE_COMPANY})

        self.submit({"type": "weekly_crawl"}, enqueue=False)
        self.clocks.advance(601)
        self.assertEqual(self.worker(SingleOnly()).maintain().orphans_enqueued, [])

    def test_shutdown_releases_the_job_and_refunds_the_attempt(self) -> None:
        runner = LoopingRunner()
        job_id = self.submit()
        stopping = threading.Event()
        thread = threading.Thread(target=self.worker(runner, stopping=stopping).process_next)
        thread.start()
        self.assertTrue(runner.started.wait(5))
        stopping.set()
        thread.join(10)
        job = self.job(job_id)
        self.assertEqual((job.status, job.attempts, job.worker_id), (JobStatus.QUEUED, 0, None))
        self.assertEqual(self.queue.stats().ready, 1)
        self.assertIn("released_on_shutdown", self.events(job_id))

    def test_run_loop_stops_promptly(self) -> None:
        stopping = threading.Event()
        worker = self.worker(CountingRunner(), stopping=stopping)
        worker._config = WorkerConfig(poll_interval=0.01, reap_interval=0.01)
        job_id = self.submit()
        thread = threading.Thread(target=worker.run)
        thread.start()
        deadline = time.monotonic() + 5
        while self.job(job_id).status is not JobStatus.COMPLETED and time.monotonic() < deadline:
            time.sleep(0.01)
        stopping.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIs(self.job(job_id).status, JobStatus.COMPLETED)


class TestWorkerPresence(WorkerHarness):
    """The heartbeat behind the dashboard's "Crawler worker offline" banner."""

    def test_a_fresh_worker_has_not_announced_itself_yet(self) -> None:
        self.worker(CountingRunner())
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 0)

    def test_heartbeat_marks_the_worker_online(self) -> None:
        self.worker(CountingRunner()).heartbeat()
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 1)

    def test_run_announces_immediately_and_retracts_on_a_clean_stop(self) -> None:
        """Online within moments of starting, offline the instant it stops."""
        stopping = threading.Event()
        worker = self.worker(CountingRunner(), stopping=stopping)
        worker._config = WorkerConfig(poll_interval=0.01, reap_interval=0.01)
        thread = threading.Thread(target=worker.run)
        thread.start()
        deadline = time.monotonic() + 5
        while self.queue.worker_presence(stale_after=90).online == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 1, "did not come online")
        stopping.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(
            self.queue.worker_presence(stale_after=90).online, 0, "a clean shutdown must retract presence"
        )

    def test_a_killed_worker_goes_stale_rather_than_retracting(self) -> None:
        """Nothing runs on a kill, so presence has to expire by itself."""
        self.worker(CountingRunner()).heartbeat()
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 1)
        self.clocks.advance(91)
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 0)

    def test_two_workers_are_counted_separately(self) -> None:
        self.worker(CountingRunner(), worker_id="worker-a").heartbeat()
        self.worker(CountingRunner(), worker_id="worker-b").heartbeat()
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 2)

    def test_a_queue_that_refuses_heartbeats_does_not_stop_the_worker(self) -> None:
        """Presence is a hint. Losing it must never cost a crawl."""

        class RefusingQueue(InMemoryJobQueue):
            def heartbeat_worker(self, worker_id: str) -> None:
                raise ConnectionError("redis is down")

        self.queue = RefusingQueue(clock=self.clocks.epoch)
        job_id = self.submit()
        worker = self.worker(CountingRunner())
        worker.heartbeat()  # must not raise
        worker.process_next()
        self.assertIs(self.job(job_id).status, JobStatus.COMPLETED)


class TestWorkerOnPostgresAndRedis(PostgresTestCase):
    """The same guarantees on real PostgreSQL and the Redis queue scripts."""

    def setUp(self) -> None:
        import fakeredis

        self.truncate()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.service = JobService(self.pg_repository)
        self.queue_clock = [time.time()]
        self.queue = RedisJobQueue(
            fakeredis.FakeRedis(decode_responses=True), prefix=f"t:{uuid.uuid4().hex}", clock=lambda: self.queue_clock[0]
        )
        self.storage = LocalFileStorage(self.scratch / "results")
        self.policy = RetryPolicy(max_attempts=3, base_delay_seconds=5)

    def worker(self, runner, worker_id="pg-worker"):
        executor = JobExecutor(
            self.service, runner, worker_id=worker_id, lease_seconds=60, heartbeat_interval=1000,
            retry_policy=self.policy, result_writer=ResultWriter(self.storage), runtime_root=self.scratch / "runtime",
            on_requeue=lambda job_id, delay: self.queue.enqueue(job_id, delay_seconds=delay), cancel_check_interval=0,
        )
        return Worker(self.service, self.queue, executor, retry_policy=self.policy)

    def submit(self) -> str:
        job = self.service.create_job(
            parse_job_request({"type": "bulk_companies", "companies": [{"website": "a.com"}, {"website": "b.com"}]}),
            owner_id=OWNER,
            max_attempts=3,
        )
        self.queue.enqueue(job.job_id)
        return job.job_id

    def expire(self, job_id: str) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute("update careercloud.jobs set lease_expires_at = now() - interval '1 second' where id = %s", [job_id])

    def test_end_to_end_with_results_and_owner_scoped_reads(self) -> None:
        job_id = self.submit()
        self.worker(FakeRunner()).process_next()
        job = self.service.get_job(job_id, owner_id=OWNER)
        self.assertEqual((job.status, job.progress.completed, job.progress.total), (JobStatus.COMPLETED, 2, 2))
        self.assertEqual(len(self.service.list_results(job_id, owner_id=OWNER)), 2)
        with self.assertRaises(Exception):
            self.service.get_job(job_id, owner_id=str(uuid.uuid4()))

    def test_crash_recovery_and_fencing(self) -> None:
        job_id = self.submit()
        self.queue.reserve("dead", visibility_timeout=300)
        dead = self.service.claim_job(job_id, worker_id="dead", lease_seconds=60)
        self.expire(job_id)
        survivor = self.worker(CountingRunner(), worker_id="survivor")
        with self.assertLogs("cloud.shared.service", level="WARNING"):
            self.assertEqual(survivor.maintain().requeued, [job_id])
        self.queue_clock[0] += 400
        survivor.process_next()
        survivor.process_next()
        job = self.service.get_job(job_id)
        self.assertEqual((job.status, job.attempts, job.worker_id), (JobStatus.COMPLETED, 2, "survivor"))
        self.assertIsNone(self.service.finish_failed(dead, "zombie"))
        self.assertIs(self.service.get_job(job_id).status, JobStatus.COMPLETED)

    def test_bounded_retries(self) -> None:
        job_id = self.submit()
        runner = FlakyRunner(failures=99)
        worker = self.worker(runner)
        for _ in range(3):
            with self.assertLogs("cloud.worker.executor", level="ERROR"):
                worker.process_next()
            self.queue_clock[0] += 1000
        self.assertFalse(worker.process_next())
        job = self.service.get_job(job_id)
        self.assertEqual((job.status, job.attempts, runner.calls), (JobStatus.FAILED, 3, 3))


if __name__ == "__main__":
    unittest.main()
