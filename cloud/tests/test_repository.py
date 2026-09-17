"""InMemoryJobRepository: storage, ordering, counting and compare-and-set."""

from __future__ import annotations

import threading
import unittest
from datetime import timedelta

from cloud.shared.models import Job, JobStatus, JobType
from cloud.shared.repository import DuplicateJobError, InMemoryJobRepository, JobRepository
from cloud.tests._helpers import START


def _job(job_id: str, status: JobStatus = JobStatus.QUEUED, minutes: int = 0) -> Job:
    return Job(
        job_id=job_id,
        type=JobType.WEEKLY_CRAWL,
        status=status,
        created_at=START + timedelta(minutes=minutes),
    )


class TestInterface(unittest.TestCase):
    def test_the_interface_cannot_be_instantiated(self) -> None:
        with self.assertRaises(TypeError):
            JobRepository()  # type: ignore[abstract]

    def test_the_in_memory_store_implements_it(self) -> None:
        self.assertIsInstance(InMemoryJobRepository(), JobRepository)


class TestAddAndGet(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryJobRepository()

    def test_a_stored_job_comes_back(self) -> None:
        job = _job("a")
        self.repo.add(job)
        self.assertEqual(self.repo.get("a"), job)

    def test_a_missing_job_is_none(self) -> None:
        self.assertIsNone(self.repo.get("nope"))

    def test_a_duplicate_id_is_refused_and_the_original_kept(self) -> None:
        self.repo.add(_job("a", JobStatus.QUEUED))
        with self.assertRaises(DuplicateJobError):
            self.repo.add(_job("a", JobStatus.RUNNING))
        self.assertIs(self.repo.get("a").status, JobStatus.QUEUED)

    def test_stored_jobs_cannot_be_mutated_by_a_caller(self) -> None:
        self.repo.add(_job("a"))
        with self.assertRaises(Exception):
            self.repo.get("a").status = JobStatus.COMPLETED  # type: ignore[misc]
        self.assertIs(self.repo.get("a").status, JobStatus.QUEUED)


class TestListAndCount(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryJobRepository()
        self.repo.add(_job("first", JobStatus.QUEUED))
        self.repo.add(_job("second", JobStatus.QUEUED))
        self.repo.add(_job("third", JobStatus.QUEUED))
        running = self.repo.get("second").model_copy(update={"status": JobStatus.RUNNING})
        self.repo.compare_and_set(running, expected_status=JobStatus.QUEUED)

    def test_newest_first(self) -> None:
        self.assertEqual([j.job_id for j in self.repo.list()], ["third", "second", "first"])

    def test_ordering_does_not_depend_on_distinct_timestamps(self) -> None:
        repo = InMemoryJobRepository()
        for name in ("x", "y", "z"):
            repo.add(_job(name))  # identical created_at
        self.assertEqual([j.job_id for j in repo.list()], ["z", "y", "x"])

    def test_filter_by_status(self) -> None:
        self.assertEqual([j.job_id for j in self.repo.list(status=JobStatus.RUNNING)], ["second"])
        self.assertEqual(self.repo.count(status=JobStatus.QUEUED), 2)

    def test_limit_and_offset(self) -> None:
        self.assertEqual([j.job_id for j in self.repo.list(limit=1, offset=1)], ["second"])
        self.assertEqual(self.repo.list(offset=10), [])

    def test_negative_paging_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.list(limit=-1)

    def test_count_by_status_includes_every_status(self) -> None:
        counts = self.repo.count_by_status()
        self.assertEqual(set(counts), set(JobStatus))
        self.assertEqual(counts[JobStatus.QUEUED], 2)
        self.assertEqual(counts[JobStatus.RUNNING], 1)
        self.assertEqual(counts[JobStatus.FAILED], 0)
        self.assertEqual(self.repo.count(), 3)


class TestCompareAndSet(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryJobRepository()
        self.repo.add(_job("a"))

    def test_succeeds_when_the_status_is_as_expected(self) -> None:
        running = self.repo.get("a").model_copy(update={"status": JobStatus.RUNNING})
        self.assertTrue(self.repo.compare_and_set(running, expected_status=JobStatus.QUEUED))
        self.assertIs(self.repo.get("a").status, JobStatus.RUNNING)

    def test_refuses_when_the_status_has_moved_on(self) -> None:
        done = self.repo.get("a").model_copy(update={"status": JobStatus.COMPLETED})
        self.assertFalse(self.repo.compare_and_set(done, expected_status=JobStatus.RUNNING))
        self.assertIs(self.repo.get("a").status, JobStatus.QUEUED)

    def test_refuses_a_job_that_was_never_added(self) -> None:
        self.assertFalse(self.repo.compare_and_set(_job("ghost"), expected_status=JobStatus.QUEUED))
        self.assertIsNone(self.repo.get("ghost"))

    def test_only_one_of_many_racing_writers_wins(self) -> None:
        winners = []
        barrier = threading.Barrier(16)

        def attempt(n: int) -> None:
            update = self.repo.get("a").model_copy(
                update={"status": JobStatus.RUNNING, "error": str(n)}
            )
            barrier.wait()
            if self.repo.compare_and_set(update, expected_status=JobStatus.QUEUED):
                winners.append(n)

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(winners), 1)
        self.assertEqual(self.repo.get("a").error, str(winners[0]))


if __name__ == "__main__":
    unittest.main()
