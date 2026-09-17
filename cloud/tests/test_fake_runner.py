"""FakeRunner, JobExecutor and the dispatchers: a job always ends somewhere final."""

from __future__ import annotations

import threading
import unittest
from typing import List, Optional, Tuple

from cloud.shared.models import JobStatus
from cloud.shared.schemas import parse_job_request
from cloud.tests._helpers import make_service, single
from cloud.worker.dispatcher import InlineDispatcher, NullDispatcher
from cloud.worker.executor import JobExecutor
from cloud.worker.fake_runner import FakeRunner
from cloud.worker.runner import JobRunner, RunOutcome, RunResult


class RecordingContext:
    def __init__(self, cancel_after_reports: Optional[int] = None) -> None:
        self.reports: List[Tuple[int, Optional[int], Optional[str]]] = []
        self._cancel_after = cancel_after_reports

    def report_progress(self, completed, total=None, message=None) -> None:
        self.reports.append((completed, total, message))

    def is_cancelled(self) -> bool:
        return self._cancel_after is not None and len(self.reports) >= self._cancel_after


class TestRunResult(unittest.TestCase):
    def test_a_failure_must_say_why(self) -> None:
        with self.assertRaises(ValueError):
            RunResult(RunOutcome.FAILED)
        self.assertEqual(RunResult.failed("403").error, "403")


class TestFakeRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()

    def test_one_step_per_target_then_completed(self) -> None:
        job = self.service.create_job(
            parse_job_request(
                {"type": "bulk_companies", "companies": [{"website": "a.com"}, {"company_name": "Bee"}]}
            )
        )
        context = RecordingContext()
        result = FakeRunner().run(job, context)

        self.assertIs(result.outcome, RunOutcome.COMPLETED)
        self.assertTrue(result.summary["simulated"])
        self.assertEqual(
            context.reports,
            [(0, 2, "Crawling a.com"), (1, 2, "Crawling Bee"), (2, 2, "Finished (simulated)")],
        )

    def test_a_job_without_targets_uses_the_configured_steps(self) -> None:
        job = self.service.create_job(parse_job_request({"type": "weekly_crawl"}))
        context = RecordingContext()
        FakeRunner(steps=5).run(job, context)
        self.assertEqual(context.reports[-1][:2], (5, 5))

    def test_it_sleeps_between_steps_through_the_injected_sleep(self) -> None:
        naps: List[float] = []
        job = self.service.create_job(single())
        FakeRunner(step_seconds=0.25, sleep=naps.append).run(job, RecordingContext())
        self.assertEqual(naps, [0.25])

    def test_it_stops_when_cancelled(self) -> None:
        job = self.service.create_job(parse_job_request({"type": "weekly_crawl"}))
        context = RecordingContext(cancel_after_reports=1)
        self.assertIs(FakeRunner(steps=10).run(job, context).outcome, RunOutcome.CANCELLED)
        self.assertEqual(len(context.reports), 1)

    def test_configured_failure(self) -> None:
        job = self.service.create_job(single())
        result = FakeRunner(fail_with="simulated 403").run(job, RecordingContext())
        self.assertEqual((result.outcome, result.error), (RunOutcome.FAILED, "simulated 403"))

    def test_bad_configuration_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            FakeRunner(step_seconds=-1)
        with self.assertRaises(ValueError):
            FakeRunner(steps=0)


class TestExecutor(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()

    def test_success_ends_completed(self) -> None:
        job_id = self.service.create_job(single()).job_id
        job = JobExecutor(self.service, FakeRunner()).execute(job_id)
        self.assertIs(job.status, JobStatus.COMPLETED)
        self.assertEqual((job.progress.completed, job.progress.total), (1, 1))
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.completed_at)

    def test_a_reported_failure_ends_failed(self) -> None:
        job_id = self.service.create_job(single()).job_id
        job = JobExecutor(self.service, FakeRunner(fail_with="simulated 403")).execute(job_id)
        self.assertIs(job.status, JobStatus.FAILED)
        self.assertEqual(job.error, "simulated 403")

    def test_a_runner_that_raises_ends_failed_not_stuck_running(self) -> None:
        job_id = self.service.create_job(single()).job_id
        with self.assertLogs("cloud.worker.executor", level="ERROR"):
            job = JobExecutor(self.service, FakeRunner(raise_error=RuntimeError("kaboom"))).execute(job_id)
        self.assertIs(job.status, JobStatus.FAILED)
        self.assertEqual(job.error, "RuntimeError: kaboom")

    def test_a_job_cancelled_before_it_starts_is_not_run(self) -> None:
        job_id = self.service.create_job(single()).job_id
        self.service.cancel_job(job_id)

        class MustNotRun(JobRunner):
            def run(self, job, context):
                raise AssertionError("ran a cancelled job")

        self.assertIs(JobExecutor(self.service, MustNotRun()).execute(job_id).status, JobStatus.CANCELLED)

    def test_a_cancel_during_the_run_stands(self) -> None:
        service = self.service
        job_id = service.create_job(parse_job_request({"type": "weekly_crawl"})).job_id

        class CancelledMidway(JobRunner):
            def run(self, job, context):
                context.report_progress(1, 3, "one")
                service.cancel_job(job.job_id)  # a user clicks Cancel
                context.report_progress(2, 3, "two")  # must not raise
                return RunResult.completed()  # and must not overwrite the cancel

        job = JobExecutor(service, CancelledMidway()).execute(job_id)
        self.assertIs(job.status, JobStatus.CANCELLED)

    def test_a_runner_that_stops_itself_ends_cancelled(self) -> None:
        job_id = self.service.create_job(single()).job_id

        class GivesUp(JobRunner):
            def run(self, job, context):
                return RunResult.cancelled()

        self.assertIs(JobExecutor(self.service, GivesUp()).execute(job_id).status, JobStatus.CANCELLED)


class TestDispatchers(unittest.TestCase):
    def test_null_dispatcher_leaves_the_job_queued(self) -> None:
        service = make_service()
        job_id = service.create_job(single()).job_id
        NullDispatcher().dispatch(job_id)
        self.assertIs(service.get_job(job_id).status, JobStatus.QUEUED)

    def test_inline_dispatcher_runs_jobs_in_the_background(self) -> None:
        service = make_service()
        dispatcher = InlineDispatcher(JobExecutor(service, FakeRunner()), max_concurrent=2)
        try:
            ids = [service.create_job(single(f"site{n}.com")).job_id for n in range(5)]
            for job_id in ids:
                dispatcher.dispatch(job_id)
            self.assertTrue(dispatcher.wait_idle(timeout=10))
        finally:
            dispatcher.shutdown()
        self.assertEqual({service.get_job(j).status for j in ids}, {JobStatus.COMPLETED})

    def test_inline_dispatcher_does_not_block_the_caller(self) -> None:
        service = make_service()
        release = threading.Event()

        class Blocks(JobRunner):
            def run(self, job, context):
                release.wait(timeout=10)
                return RunResult.completed()

        dispatcher = InlineDispatcher(JobExecutor(service, Blocks()))
        try:
            job_id = service.create_job(single()).job_id
            dispatcher.dispatch(job_id)
            # Had dispatch run the job itself, it would only have returned after
            # the 10s wait, with the job completed.
            self.assertIsNot(service.get_job(job_id).status, JobStatus.COMPLETED)
            release.set()
            self.assertTrue(dispatcher.wait_idle(timeout=10))
        finally:
            dispatcher.shutdown()
        self.assertIs(service.get_job(job_id).status, JobStatus.COMPLETED)

    def test_inline_dispatcher_needs_a_worker(self) -> None:
        with self.assertRaises(ValueError):
            InlineDispatcher(JobExecutor(make_service(), FakeRunner()), max_concurrent=0)


if __name__ == "__main__":
    unittest.main()
