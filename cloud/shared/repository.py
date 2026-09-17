"""Where jobs are kept.

:class:`JobRepository` is the interface; :class:`InMemoryJobRepository` is the
only implementation in Phase 5A. A PostgreSQL/Supabase repository replaces it in
Phase 5B without the service, the API or the worker changing — which is why the
interface speaks only in :class:`~cloud.shared.models.Job` values and never
hands out anything a caller could mutate in place.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections import Counter
from typing import Dict, List, Optional

from cloud.shared.models import Job, JobStatus

__all__ = ["DuplicateJobError", "InMemoryJobRepository", "JobRepository"]


class DuplicateJobError(Exception):
    """A job with this id is already stored."""


class JobRepository(ABC):
    """Persistence for jobs. Implementations must be safe to share across threads."""

    @abstractmethod
    def add(self, job: Job) -> None:
        """Store a new job. Raises :class:`DuplicateJobError` if the id is taken."""

    @abstractmethod
    def get(self, job_id: str) -> Optional[Job]:
        """The job with this id, or ``None``."""

    @abstractmethod
    def list(
        self,
        *,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Job]:
        """Jobs newest first, optionally only those in ``status``."""

    @abstractmethod
    def count(self, *, status: Optional[JobStatus] = None) -> int:
        """How many jobs :meth:`list` would page through."""

    @abstractmethod
    def count_by_status(self) -> Dict[JobStatus, int]:
        """Every status, including those with no jobs, mapped to its job count."""

    @abstractmethod
    def compare_and_set(self, job: Job, *, expected_status: JobStatus) -> bool:
        """Replace the stored job with ``job`` if its status is still ``expected_status``.

        Returns ``False`` — and stores nothing — if the job is missing or has
        moved on. This is the only way a stored job changes. In SQL it is
        ``UPDATE jobs SET ... WHERE job_id = :id AND status = :expected``.
        """


class InMemoryJobRepository(JobRepository):
    """A dict behind a lock. Forgets everything when the process exits.

    Jobs are frozen Pydantic models, so returning the stored instance is safe:
    no caller can change what another caller reads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        # Insertion order, so "newest first" does not depend on two jobs having
        # distinct created_at timestamps (a fast client can create two in one tick).
        self._order: List[str] = []

    def add(self, job: Job) -> None:
        with self._lock:
            if job.job_id in self._jobs:
                raise DuplicateJobError(job.job_id)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(
        self,
        *,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Job]:
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must not be negative")
        with self._lock:
            newest_first = [self._jobs[job_id] for job_id in reversed(self._order)]
        if status is not None:
            newest_first = [job for job in newest_first if job.status is status]
        return newest_first[offset : offset + limit]

    def count(self, *, status: Optional[JobStatus] = None) -> int:
        with self._lock:
            if status is None:
                return len(self._jobs)
            return sum(1 for job in self._jobs.values() if job.status is status)

    def count_by_status(self) -> Dict[JobStatus, int]:
        with self._lock:
            tally = Counter(job.status for job in self._jobs.values())
        return {status: tally.get(status, 0) for status in JobStatus}

    def compare_and_set(self, job: Job, *, expected_status: JobStatus) -> bool:
        with self._lock:
            current = self._jobs.get(job.job_id)
            if current is None or current.status is not expected_status:
                return False
            self._jobs[job.job_id] = job
            return True
