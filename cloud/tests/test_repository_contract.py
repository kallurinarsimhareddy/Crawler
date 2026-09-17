"""One contract, two repositories: in-memory and PostgreSQL behave the same.

Every test here runs twice — against :class:`InMemoryJobRepository` and against
:class:`PostgresJobRepository` on a real PostgreSQL 16 — so the in-memory store
used by fast tests cannot drift from the production one.
"""

from __future__ import annotations

import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from cloud.shared.models import (
    CompanyTarget,
    Job,
    JobProgress,
    JobStatus,
    JobType,
    ResultFile,
    ResultKind,
    TargetStatus,
)
from cloud.shared.repository import DuplicateJobError, InMemoryJobRepository, JobRepository
from cloud.tests._pg import PostgresTestCase

ALICE = str(uuid.uuid4())
BOB = str(uuid.uuid4())
T0 = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


def make_job(owner: str = ALICE, *, targets: int = 1, minutes: int = 0, max_attempts: int = 3) -> Job:
    items = [CompanyTarget(website=f"https://site{n}.example.com", company_name=f"Co {n}") for n in range(targets)]
    return Job(
        job_id=job_id(),
        owner_id=owner,
        type=JobType.BULK_COMPANIES if targets != 1 else JobType.SINGLE_COMPANY,
        status=JobStatus.QUEUED,
        targets=items,
        target_count=targets,
        created_at=T0 + timedelta(minutes=minutes),
        updated_at=T0 + timedelta(minutes=minutes),
        max_attempts=max_attempts,
        progress=JobProgress(total=targets or None, current_phase="queued"),
    )


class RepositoryContract:
    """Mixed into a TestCase that provides ``self.repo`` and ``expire_lease(job_id)``."""

    repo: JobRepository

    # --- storage & scope ----------------------------------------------------

    def test_add_get_round_trip_with_targets(self) -> None:
        job = make_job(targets=3)
        self.repo.add(job)
        stored = self.repo.get(job.job_id)
        self.assertEqual(stored.job_id, job.job_id)
        self.assertEqual(stored.owner_id, ALICE)
        self.assertEqual(stored.targets, job.targets)
        self.assertEqual(stored.target_count, 3)
        self.assertEqual(stored.progress.total, 3)
        self.assertEqual(stored.status, JobStatus.QUEUED)
        self.assertEqual([t.position for t in self.repo.list_targets(job.job_id)], [0, 1, 2])

    def test_duplicate_ids_are_refused(self) -> None:
        job = make_job()
        self.repo.add(job)
        with self.assertRaises(DuplicateJobError):
            self.repo.add(job)

    def test_a_user_cannot_create_a_job_for_someone_else(self) -> None:
        with self.assertRaises(PermissionError):
            self.repo.add(make_job(owner=BOB), owner_id=ALICE)

    def test_owner_scope_hides_other_users_jobs_everywhere(self) -> None:
        mine, theirs = make_job(ALICE, targets=2), make_job(BOB, targets=2)
        self.repo.add(mine, owner_id=ALICE)
        self.repo.add(theirs, owner_id=BOB)
        self.repo.add_event(theirs.job_id, "created", owner_id=BOB)

        self.assertIsNone(self.repo.get(theirs.job_id, owner_id=ALICE))
        self.assertEqual([j.job_id for j in self.repo.list(owner_id=ALICE)], [mine.job_id])
        self.assertEqual(self.repo.count(owner_id=ALICE), 1)
        self.assertEqual(self.repo.count_by_status(owner_id=ALICE)[JobStatus.QUEUED], 1)
        self.assertEqual(self.repo.list_targets(theirs.job_id, owner_id=ALICE), [])
        self.assertEqual(self.repo.list_events(theirs.job_id, owner_id=ALICE), [])
        self.assertIsNone(
            self.repo.update_where(
                theirs.job_id,
                {"cancel_requested_at": T0, "status": JobStatus.CANCELLED, "completed_at": T0},
                expected_status=JobStatus.QUEUED,
                owner_id=ALICE,
            )
        )
        self.assertIs(self.repo.get(theirs.job_id).status, JobStatus.QUEUED)
        # system scope sees both
        self.assertEqual(self.repo.count(), 2)

    def test_list_is_newest_first_and_pages(self) -> None:
        jobs = [make_job(minutes=n) for n in range(4)]
        for job in jobs:
            self.repo.add(job)
        ids = [j.job_id for j in self.repo.list(owner_id=ALICE)]
        self.assertEqual(ids, [j.job_id for j in reversed(jobs)])
        self.assertEqual([j.job_id for j in self.repo.list(limit=2, offset=1)], ids[1:3])

    # --- conditional writes --------------------------------------------------

    def test_update_where_is_conditional_and_narrow(self) -> None:
        job = make_job()
        self.repo.add(job)
        self.assertIsNone(
            self.repo.update_where(job.job_id, {"error": "x"}, expected_status=JobStatus.RUNNING)
        )
        updated = self.repo.update_where(
            job.job_id, {"cancel_requested_at": T0}, expected_status=JobStatus.QUEUED
        )
        self.assertEqual(updated.cancel_requested_at, T0)
        # a later narrow write does not clobber the cancel request
        progressed = self.repo.update_where(
            job.job_id, {"progress": JobProgress(total=1, message="hi")}, expected_status=JobStatus.QUEUED
        )
        self.assertEqual(progressed.cancel_requested_at, T0)
        self.assertEqual(progressed.progress.message, "hi")
        with self.assertRaises(ValueError):
            self.repo.update_where(job.job_id, {"owner_id": BOB}, expected_status=JobStatus.QUEUED)

    # --- claims, leases, recovery --------------------------------------------

    def test_claim_is_exclusive_under_contention(self) -> None:
        job = make_job()
        self.repo.add(job)
        winners = []
        barrier = threading.Barrier(8)

        def attempt(n: int) -> None:
            barrier.wait()
            if self.repo.claim(job.job_id, worker_id=f"w{n}", lease_seconds=60) is not None:
                winners.append(n)

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(winners), 1)
        claimed = self.repo.get(job.job_id)
        self.assertEqual((claimed.status, claimed.attempts, claimed.worker_id), (JobStatus.RUNNING, 1, f"w{winners[0]}"))
        self.assertIsNotNone(claimed.lease_expires_at)
        self.assertIsNotNone(claimed.started_at)

    def test_claim_refuses_cancelled_finished_and_exhausted_jobs(self) -> None:
        cancelled = make_job()
        self.repo.add(cancelled)
        self.repo.update_where(cancelled.job_id, {"cancel_requested_at": T0}, expected_status=JobStatus.QUEUED)
        self.assertIsNone(self.repo.claim(cancelled.job_id, worker_id="w", lease_seconds=60))

        exhausted = make_job(max_attempts=1)
        self.repo.add(exhausted)
        self.assertIsNotNone(self.repo.claim(exhausted.job_id, worker_id="w", lease_seconds=60))
        self.repo.update_where(
            exhausted.job_id,
            {"status": JobStatus.QUEUED, "worker_id": None, "lease_expires_at": None},
            expected_status=JobStatus.RUNNING,
        )
        self.assertIsNone(self.repo.claim(exhausted.job_id, worker_id="w", lease_seconds=60))
        self.assertIsNone(self.repo.claim("job_" + "0" * 32, worker_id="w", lease_seconds=60))

    def test_heartbeat_and_writes_are_fenced_on_worker_and_attempt(self) -> None:
        job = make_job()
        self.repo.add(job)
        claimed = self.repo.claim(job.job_id, worker_id="w1", lease_seconds=60)
        self.assertIsNotNone(self.repo.heartbeat(job.job_id, worker_id="w1", attempts=1, lease_seconds=60))
        self.assertIsNone(self.repo.heartbeat(job.job_id, worker_id="w2", attempts=1, lease_seconds=60))
        self.assertIsNone(self.repo.heartbeat(job.job_id, worker_id="w1", attempts=2, lease_seconds=60))
        self.assertIsNone(
            self.repo.update_where(
                job.job_id, {"error": "late"}, expected_status=JobStatus.RUNNING, worker_id="w2", attempts=1
            )
        )
        self.assertIsNotNone(
            self.repo.update_where(
                job.job_id, {"error": "mine"}, expected_status=JobStatus.RUNNING,
                worker_id=claimed.worker_id, attempts=claimed.attempts,
            )
        )

    def test_expired_leases_are_found(self) -> None:
        fresh, stale = make_job(), make_job()
        for job in (fresh, stale):
            self.repo.add(job)
            self.repo.claim(job.job_id, worker_id="w", lease_seconds=600)
        self.assertEqual(self.repo.find_stale(), [])
        self.expire_lease(stale.job_id)
        self.assertEqual([j.job_id for j in self.repo.find_stale()], [stale.job_id])
        self.assertIsNone(self.repo.heartbeat(fresh.job_id, worker_id="other", attempts=1, lease_seconds=60))

    def test_orphans_are_old_queued_jobs(self) -> None:
        job = make_job()
        self.repo.add(job)
        self.age_job(job.job_id, seconds=3600)
        self.assertEqual([j.job_id for j in self.repo.find_orphaned(older_than_seconds=600)], [job.job_id])
        self.repo.update_where(job.job_id, {}, expected_status=JobStatus.QUEUED)  # touch
        self.assertEqual(self.repo.find_orphaned(older_than_seconds=600), [])

    # --- targets, events, results --------------------------------------------

    def test_targets_record_per_company_outcomes(self) -> None:
        job = make_job(targets=2)
        self.repo.add(job)
        self.repo.update_target(
            job.job_id, 1, {"status": TargetStatus.FAILED, "platform": "Workday", "error": "403", "jobs_found": 0}
        )
        targets = self.repo.list_targets(job.job_id, owner_id=ALICE)
        self.assertEqual(targets[0].status, TargetStatus.PENDING)
        self.assertEqual((targets[1].status, targets[1].platform, targets[1].error), (TargetStatus.FAILED, "Workday", "403"))

    def test_events_are_ordered(self) -> None:
        job = make_job()
        self.repo.add(job)
        for kind in ("created", "claimed", "completed"):
            self.repo.add_event(job.job_id, kind, attempt=1, data={"k": kind})
        self.assertEqual([e.kind for e in self.repo.list_events(job.job_id, owner_id=ALICE)], ["created", "claimed", "completed"])

    def test_results_upsert_per_kind_and_are_owner_scoped(self) -> None:
        job = make_job()
        self.repo.add(job)

        def result(size: int) -> ResultFile:
            return ResultFile(
                result_id=f"res_{uuid.uuid4().hex}",
                job_id=job.job_id,
                owner_id=ALICE,
                kind=ResultKind.JOBS_CSV,
                filename="jobs.csv",
                content_type="text/csv",
                storage_key=f"results/{ALICE}/{job.job_id}/jobs.csv",
                size_bytes=size,
                sha256="a" * 64,
                row_count=size,
                created_at=T0,
            )

        first = self.repo.upsert_result(result(10))
        second = self.repo.upsert_result(result(20))
        self.assertEqual(first.result_id, second.result_id)
        listed = self.repo.list_results(job.job_id, owner_id=ALICE)
        self.assertEqual([(r.kind, r.size_bytes) for r in listed], [(ResultKind.JOBS_CSV, 20)])
        self.assertIsNotNone(self.repo.get_result(job.job_id, first.result_id, owner_id=ALICE))
        self.assertIsNone(self.repo.get_result(job.job_id, first.result_id, owner_id=BOB))
        self.assertEqual(self.repo.list_results(job.job_id, owner_id=BOB), [])


class TestInMemoryRepository(RepositoryContract, unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.repo = InMemoryJobRepository(clock=lambda: self.now)

    def expire_lease(self, job_id: str) -> None:
        job = self.repo.get(job_id)
        self.repo.update_where(
            job_id, {"lease_expires_at": self.now - timedelta(seconds=1)}, expected_status=JobStatus.RUNNING
        )
        self.assertIsNotNone(job)

    def age_job(self, job_id: str, *, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class TestPostgresRepository(RepositoryContract, PostgresTestCase):
    def setUp(self) -> None:
        self.truncate()
        self.repo = self.pg_repository

    def _sql(self, statement: str, params=None) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute(statement, params)

    def expire_lease(self, job_id: str) -> None:
        self._sql(
            "update careercloud.jobs set lease_expires_at = now() - interval '1 second' where id = %s", [job_id]
        )

    def age_job(self, job_id: str, *, seconds: float) -> None:
        # The trigger stamps updated_at on every UPDATE, so age it with the trigger disabled.
        self._sql("alter table careercloud.jobs disable trigger jobs_guard_update")
        try:
            self._sql(
                "update careercloud.jobs set updated_at = now() - make_interval(secs => %s) where id = %s",
                [seconds, job_id],
            )
        finally:
            self._sql("alter table careercloud.jobs enable trigger jobs_guard_update")


if __name__ == "__main__":
    unittest.main()
