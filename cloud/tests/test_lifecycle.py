"""JobService: every legal move, every refused one, and the races between them."""

from __future__ import annotations

import unittest
from datetime import timedelta

from cloud.shared.models import ALLOWED_TRANSITIONS, TERMINAL_STATUSES, JobStatus, JobType, can_transition
from cloud.shared.schemas import parse_job_request
from cloud.shared.service import MAX_ERROR_LENGTH, InvalidTransitionError, JobNotFoundError
from cloud.tests._helpers import START, make_service, single


class TestTransitionTable(unittest.TestCase):
    def test_terminal_statuses_go_nowhere(self) -> None:
        for status in TERMINAL_STATUSES:
            self.assertEqual(ALLOWED_TRANSITIONS[status], frozenset())

    def test_no_status_may_transition_to_itself(self) -> None:
        for status in JobStatus:
            self.assertFalse(can_transition(status, status))

    def test_a_completed_job_cannot_be_rerun(self) -> None:
        self.assertFalse(can_transition(JobStatus.COMPLETED, JobStatus.RUNNING))
        self.assertFalse(can_transition(JobStatus.QUEUED, JobStatus.COMPLETED))


class TestCreate(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()

    def test_a_new_job_is_queued_with_its_target(self) -> None:
        job = self.service.create_job(single("Example.com"))
        self.assertEqual(job.job_id, "job_0001")
        self.assertIs(job.type, JobType.SINGLE_COMPANY)
        self.assertIs(job.status, JobStatus.QUEUED)
        self.assertEqual(job.created_at, START)
        self.assertIsNone(job.started_at)
        self.assertEqual(job.target_label(), "example.com")
        self.assertEqual(self.service.get_job("job_0001"), job)

    def test_target_labels_per_type(self) -> None:
        weekly = self.service.create_job(parse_job_request({"type": "weekly_crawl"}))
        bulk = self.service.create_job(
            parse_job_request(
                {"type": "bulk_companies", "companies": [{"website": "a.com"}, {"website": "b.com"}]}
            )
        )
        named = self.service.create_job(
            parse_job_request({"type": "discovery", "company_name": "Acme", "website": "acme.com"})
        )
        self.assertEqual(weekly.target_label(), "Weekly roster")
        self.assertEqual(bulk.target_label(), "2 companies")
        self.assertEqual(named.target_label(), "Acme")

    def test_an_unknown_job_is_not_found(self) -> None:
        with self.assertRaises(JobNotFoundError):
            self.service.get_job("job_missing")


class TestHappyPath(unittest.TestCase):
    def test_queued_running_completed(self) -> None:
        service = make_service()
        job = service.create_job(single())

        running = service.start_job(job.job_id, total=4)
        self.assertIs(running.status, JobStatus.RUNNING)
        self.assertEqual(running.started_at, START + timedelta(seconds=1))
        self.assertEqual((running.progress.completed, running.progress.total), (0, 4))

        service.report_progress(job.job_id, completed=2, total=4, message="halfway")
        self.assertEqual(service.get_job(job.job_id).progress.message, "halfway")

        done = service.complete_job(job.job_id, message="Completed")
        self.assertIs(done.status, JobStatus.COMPLETED)
        self.assertEqual(done.completed_at, START + timedelta(seconds=2))
        self.assertEqual((done.progress.completed, done.progress.total), (4, 4))
        self.assertIsNone(done.error)
        self.assertTrue(done.is_terminal)

    def test_running_to_failed_records_the_error(self) -> None:
        service = make_service()
        job = service.create_job(single())
        service.start_job(job.job_id)
        failed = service.fail_job(job.job_id, "  board returned 403  ")
        self.assertIs(failed.status, JobStatus.FAILED)
        self.assertEqual(failed.error, "board returned 403")
        self.assertIsNotNone(failed.completed_at)

    def test_a_blank_error_is_still_an_error(self) -> None:
        service = make_service()
        job = service.create_job(single())
        service.start_job(job.job_id)
        self.assertEqual(service.fail_job(job.job_id, "").error, "unknown error")

    def test_an_enormous_error_is_truncated(self) -> None:
        service = make_service()
        job = service.create_job(single())
        service.start_job(job.job_id)
        error = service.fail_job(job.job_id, "x" * 50_000).error
        self.assertEqual(len(error), MAX_ERROR_LENGTH)
        self.assertTrue(error.endswith("…"))

    def test_cancel_from_queued_and_from_running(self) -> None:
        service = make_service()
        queued = service.create_job(single())
        running = service.create_job(single())
        service.start_job(running.job_id)
        self.assertIs(service.cancel_job(queued.job_id).status, JobStatus.CANCELLED)
        self.assertIs(service.cancel_job(running.job_id).status, JobStatus.CANCELLED)
        self.assertTrue(service.is_cancelled(running.job_id))


class TestRefusedMoves(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.job_id = self.service.create_job(single()).job_id

    def test_cannot_complete_or_fail_a_job_that_never_started(self) -> None:
        with self.assertRaises(InvalidTransitionError):
            self.service.complete_job(self.job_id)
        with self.assertRaises(InvalidTransitionError):
            self.service.fail_job(self.job_id, "boom")
        self.assertIs(self.service.get_job(self.job_id).status, JobStatus.QUEUED)

    def test_cannot_start_twice(self) -> None:
        self.service.start_job(self.job_id)
        with self.assertRaises(InvalidTransitionError) as caught:
            self.service.start_job(self.job_id)
        self.assertIs(caught.exception.current, JobStatus.RUNNING)

    def test_terminal_jobs_are_final(self) -> None:
        self.service.start_job(self.job_id)
        self.service.complete_job(self.job_id)
        for move in (
            lambda: self.service.start_job(self.job_id),
            lambda: self.service.fail_job(self.job_id, "late"),
            lambda: self.service.cancel_job(self.job_id),
            lambda: self.service.complete_job(self.job_id),
        ):
            with self.assertRaises(InvalidTransitionError):
                move()
        self.assertIs(self.service.get_job(self.job_id).status, JobStatus.COMPLETED)

    def test_progress_is_refused_unless_running(self) -> None:
        with self.assertRaises(InvalidTransitionError):
            self.service.report_progress(self.job_id, completed=1)
        self.service.start_job(self.job_id)
        self.service.cancel_job(self.job_id)
        with self.assertRaises(InvalidTransitionError):
            self.service.report_progress(self.job_id, completed=1)

    def test_a_missing_job_cannot_move(self) -> None:
        with self.assertRaises(JobNotFoundError):
            self.service.cancel_job("job_missing")


class TestRace(unittest.TestCase):
    def test_a_cancel_between_read_and_write_wins(self) -> None:
        """A worker that read ``running`` must not overwrite a cancel that landed since."""
        service = make_service()
        job_id = service.create_job(single()).job_id
        service.start_job(job_id)
        stale = service.get_job(job_id)
        service.cancel_job(job_id)

        with self.assertRaises(InvalidTransitionError) as caught:
            service._transition(job_id, JobStatus.COMPLETED, expected=stale)
        self.assertIs(caught.exception.current, JobStatus.CANCELLED)
        self.assertIs(service.get_job(job_id).status, JobStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
