"""The rules of a job's life, in one place.

:class:`JobService` is the only thing that creates jobs or changes their status.
The API calls it for requests; the worker calls it as a run progresses. Neither
writes to the repository directly, so the transition table in
:mod:`cloud.shared.models` cannot be bypassed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from cloud.shared.models import (
    Job,
    JobProgress,
    JobStatus,
    JobType,
    can_transition,
)
from cloud.shared.repository import JobRepository
from cloud.shared.schemas import JobCreateRequest, request_targets

__all__ = [
    "InvalidTransitionError",
    "JobNotFoundError",
    "JobService",
]

#: Longest error message stored on a job. A crawler traceback can be enormous;
#: the job record is a status, not a log.
MAX_ERROR_LENGTH = 2000


class JobNotFoundError(LookupError):
    """No job has this id."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"job {job_id!r} not found")
        self.job_id = job_id


class InvalidTransitionError(Exception):
    """The job cannot move from its current status to the one requested."""

    def __init__(self, job_id: str, current: JobStatus, requested: JobStatus) -> None:
        super().__init__(
            f"job {job_id!r} is {current.value} and cannot become {requested.value}"
        )
        self.job_id = job_id
        self.current = current
        self.requested = requested


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


class JobService:
    """Create jobs and move them through their lifecycle.

    Args:
        repository: Where jobs are stored.
        clock: Returns the current time, timezone-aware. Injected for tests.
        id_factory: Returns a fresh job id. Injected for tests.
    """

    def __init__(
        self,
        repository: JobRepository,
        *,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_job_id,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._id_factory = id_factory

    # --- reads ---------------------------------------------------------------

    def get_job(self, job_id: str) -> Job:
        job = self._repository.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def list_jobs(
        self,
        *,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Job]:
        return self._repository.list(status=status, limit=limit, offset=offset)

    def count_jobs(self, *, status: Optional[JobStatus] = None) -> int:
        return self._repository.count(status=status)

    def counts_by_status(self) -> Dict[JobStatus, int]:
        return self._repository.count_by_status()

    # --- lifecycle -----------------------------------------------------------

    def create_job(self, request: JobCreateRequest) -> Job:
        """Record a validated request as a queued job."""
        job = Job(
            job_id=self._id_factory(),
            type=JobType(request.type),
            status=JobStatus.QUEUED,
            targets=request_targets(request),
            created_at=self._clock(),
        )
        self._repository.add(job)
        return job

    def start_job(self, job_id: str, *, total: Optional[int] = None) -> Job:
        """queued -> running."""
        return self._transition(
            job_id,
            JobStatus.RUNNING,
            started_at=self._clock(),
            progress=JobProgress(completed=0, total=total, message="Starting"),
        )

    def complete_job(self, job_id: str, *, message: Optional[str] = None) -> Job:
        """running -> completed."""
        current = self.get_job(job_id)
        progress = current.progress
        if message is not None or progress.total is not None:
            progress = JobProgress(
                completed=progress.total if progress.total is not None else progress.completed,
                total=progress.total,
                message=message if message is not None else progress.message,
            )
        return self._transition(
            job_id,
            JobStatus.COMPLETED,
            expected=current,
            completed_at=self._clock(),
            progress=progress,
        )

    def fail_job(self, job_id: str, error: str) -> Job:
        """running -> failed, recording why."""
        text = (error or "").strip() or "unknown error"
        if len(text) > MAX_ERROR_LENGTH:
            text = text[: MAX_ERROR_LENGTH - 1] + "…"
        return self._transition(
            job_id, JobStatus.FAILED, completed_at=self._clock(), error=text
        )

    def cancel_job(self, job_id: str) -> Job:
        """queued or running -> cancelled."""
        return self._transition(job_id, JobStatus.CANCELLED, completed_at=self._clock())

    def report_progress(
        self,
        job_id: str,
        *,
        completed: int,
        total: Optional[int] = None,
        message: Optional[str] = None,
    ) -> Job:
        """Update a running job's progress. Refused once the job has left ``running``."""
        current = self.get_job(job_id)
        if current.status is not JobStatus.RUNNING:
            raise InvalidTransitionError(job_id, current.status, JobStatus.RUNNING)
        updated = current.model_copy(
            update={
                "progress": JobProgress(completed=completed, total=total, message=message)
            }
        )
        if not self._repository.compare_and_set(updated, expected_status=JobStatus.RUNNING):
            latest = self.get_job(job_id)
            raise InvalidTransitionError(job_id, latest.status, JobStatus.RUNNING)
        return updated

    def is_cancelled(self, job_id: str) -> bool:
        return self.get_job(job_id).status is JobStatus.CANCELLED

    # --- internals -----------------------------------------------------------

    def _transition(
        self,
        job_id: str,
        new_status: JobStatus,
        *,
        expected: Optional[Job] = None,
        **changes: object,
    ) -> Job:
        current = expected if expected is not None else self.get_job(job_id)
        if not can_transition(current.status, new_status):
            raise InvalidTransitionError(job_id, current.status, new_status)
        updated = current.model_copy(update={"status": new_status, **changes})
        if not self._repository.compare_and_set(updated, expected_status=current.status):
            # Someone else moved the job between our read and our write — a
            # cancel racing a finishing worker, typically. Report what it is now.
            latest = self.get_job(job_id)
            raise InvalidTransitionError(job_id, latest.status, new_status)
        return updated
