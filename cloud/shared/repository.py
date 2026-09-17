"""Where jobs are kept.

:class:`JobRepository` is the interface. :class:`InMemoryJobRepository` is the
local-development and test implementation;
:class:`cloud.db.postgres.PostgresJobRepository` is the real one. The API, the
service and the worker only ever see the interface.

**Scope.** Every read and every user-initiated write takes an ``owner_id``.
Given one, the repository behaves as that user: it can see and touch only that
user's jobs (in PostgreSQL this is enforced by row-level security as well as by
the query). ``owner_id=None`` is *system* scope, which only the worker uses — it
has to claim and update jobs regardless of who owns them. The API never passes
``None``; ``cloud/tests/test_ownership.py`` holds it to that.

**Writes are narrow.** :meth:`JobRepository.update_where` changes only the
columns it is given, and only if the job is still in the expected status (and,
for a worker, still on the attempt it claimed). A worker's progress update can
therefore never overwrite a cancel request that landed a moment earlier.
"""

from __future__ import annotations

import itertools
import threading
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Tuple, Union

from cloud.shared.models import (
    Job,
    JobEvent,
    JobStatus,
    ResultFile,
    TargetRecord,
)

__all__ = ["DuplicateJobError", "InMemoryJobRepository", "JobRepository", "StatusFilter"]

StatusFilter = Union[JobStatus, Collection[JobStatus]]

#: Job fields a caller may change through update_where. Identity, ownership,
#: type, targets and creation time are fixed at creation.
MUTABLE_JOB_FIELDS = frozenset(
    {
        "status",
        "started_at",
        "completed_at",
        "updated_at",
        "error",
        "progress",
        "attempts",
        "max_attempts",
        "worker_id",
        "heartbeat_at",
        "lease_expires_at",
        "cancel_requested_at",
    }
)


class DuplicateJobError(Exception):
    """A job with this id is already stored."""


def _statuses(expected: StatusFilter) -> frozenset:
    if isinstance(expected, JobStatus):
        return frozenset({expected})
    return frozenset(expected)


class JobRepository(ABC):
    """Persistence for jobs. Implementations must be safe to share across threads."""

    #: Reported by the health endpoint.
    name: str = "repository"

    # --- jobs ----------------------------------------------------------------

    @abstractmethod
    def add(self, job: Job, *, owner_id: Optional[str] = None) -> None:
        """Store a new job and one pending target row per ``job.targets`` entry.

        Raises :class:`DuplicateJobError` if the id is taken, and
        :class:`PermissionError` if ``owner_id`` is given and is not the job's owner.
        """

    @abstractmethod
    def get(self, job_id: str, *, owner_id: Optional[str] = None) -> Optional[Job]:
        """The job with this id, or ``None`` — also ``None`` if another user owns it."""

    @abstractmethod
    def list(
        self,
        *,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
        owner_id: Optional[str] = None,
    ) -> List[Job]:
        """Jobs newest first, optionally only those in ``status``."""

    @abstractmethod
    def count(self, *, status: Optional[JobStatus] = None, owner_id: Optional[str] = None) -> int:
        """How many jobs :meth:`list` would page through."""

    @abstractmethod
    def count_by_status(self, *, owner_id: Optional[str] = None) -> Dict[JobStatus, int]:
        """Every status, including those with no jobs, mapped to its job count."""

    @abstractmethod
    def update_where(
        self,
        job_id: str,
        changes: Mapping[str, Any],
        *,
        expected_status: StatusFilter,
        owner_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        attempts: Optional[int] = None,
    ) -> Optional[Job]:
        """Apply ``changes`` if the job is in ``expected_status`` (and, when given,
        is owned by ``owner_id``, held by ``worker_id`` on attempt ``attempts``).

        Returns the updated job, or ``None`` if any condition failed — in which
        case nothing was written. ``updated_at`` is always refreshed.
        """

    def compare_and_set(self, job: Job, *, expected_status: JobStatus) -> bool:
        """Replace every mutable field with ``job``'s if the status is still expected.

        The Phase 5A primitive, kept for compatibility. It writes *all* mutable
        fields, so it is only safe where nothing else writes concurrently;
        everything new uses :meth:`update_where`.
        """
        changes = {name: getattr(job, name) for name in MUTABLE_JOB_FIELDS if name != "updated_at"}
        return self.update_where(job.job_id, changes, expected_status=expected_status) is not None

    # --- worker-only (system scope) ------------------------------------------

    @abstractmethod
    def claim(self, job_id: str, *, worker_id: str, lease_seconds: float) -> Optional[Job]:
        """Atomically move a queued job to running for ``worker_id``.

        Succeeds only if the job is queued, has no cancel request, and has
        attempts left. Increments ``attempts`` and sets a lease. ``None`` means
        someone else has it, it is finished, or it has run out of attempts.
        """

    @abstractmethod
    def heartbeat(
        self, job_id: str, *, worker_id: str, attempts: int, lease_seconds: float
    ) -> Optional[Job]:
        """Extend the lease of a running job this worker holds. ``None`` = lease lost."""

    @abstractmethod
    def find_stale(self, *, limit: int = 100) -> List[Job]:
        """Running jobs whose lease has expired: their worker stopped heartbeating."""

    @abstractmethod
    def find_orphaned(self, *, older_than_seconds: float, limit: int = 100) -> List[Job]:
        """Queued jobs untouched for ``older_than_seconds`` — maybe lost by the queue."""

    # --- targets -------------------------------------------------------------

    @abstractmethod
    def list_targets(self, job_id: str, *, owner_id: Optional[str] = None) -> List[TargetRecord]:
        """The job's companies in request order; empty if not visible."""

    @abstractmethod
    def update_target(self, job_id: str, position: int, changes: Mapping[str, Any]) -> None:
        """Record how one company went. System scope."""

    # --- events --------------------------------------------------------------

    @abstractmethod
    def add_event(
        self,
        job_id: str,
        kind: str,
        *,
        message: Optional[str] = None,
        attempt: Optional[int] = None,
        data: Optional[Mapping[str, Any]] = None,
        owner_id: Optional[str] = None,
    ) -> None:
        """Append to the job's timeline."""

    @abstractmethod
    def list_events(
        self, job_id: str, *, owner_id: Optional[str] = None, limit: int = 200
    ) -> List[JobEvent]:
        """The job's timeline, oldest first; empty if not visible."""

    # --- results -------------------------------------------------------------

    @abstractmethod
    def upsert_result(self, result: ResultFile) -> ResultFile:
        """Store result metadata. One row per (job, kind): a re-run replaces it."""

    @abstractmethod
    def list_results(self, job_id: str, *, owner_id: Optional[str] = None) -> List[ResultFile]:
        """The job's results; empty if not visible."""

    @abstractmethod
    def get_result(
        self, job_id: str, result_id: str, *, owner_id: Optional[str] = None
    ) -> Optional[ResultFile]:
        """One result of one job, or ``None`` if missing or not visible."""

    # --- lifecycle -----------------------------------------------------------

    def ping(self) -> None:
        """Raise if the store is unreachable."""

    def close(self) -> None:
        """Release connections."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryJobRepository(JobRepository):
    """Dicts behind a lock. Forgets everything when the process exits.

    Behaves like the PostgreSQL repository, including owner scoping, so tests
    written against it describe the real contract. Jobs are frozen Pydantic
    models, so returning a stored instance is safe.
    """

    name = "memory"

    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        # Insertion order, so "newest first" does not depend on two jobs having
        # distinct created_at timestamps (a fast client can create two in one tick).
        self._order: List[str] = []
        self._targets: Dict[str, List[TargetRecord]] = {}
        self._events: Dict[str, List[JobEvent]] = {}
        self._results: Dict[Tuple[str, str], ResultFile] = {}
        self._event_ids = itertools.count(1)

    def _visible(self, job_id: str, owner_id: Optional[str]) -> Optional[Job]:
        job = self._jobs.get(job_id)
        if job is None or (owner_id is not None and job.owner_id != owner_id):
            return None
        return job

    # --- jobs ----------------------------------------------------------------

    def add(self, job: Job, *, owner_id: Optional[str] = None) -> None:
        if owner_id is not None and job.owner_id != owner_id:
            raise PermissionError("a user may only create their own jobs")
        with self._lock:
            if job.job_id in self._jobs:
                raise DuplicateJobError(job.job_id)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._targets[job.job_id] = [
                TargetRecord(
                    job_id=job.job_id,
                    position=index,
                    website=target.website,
                    company_name=target.company_name,
                )
                for index, target in enumerate(job.targets)
            ]

    def get(self, job_id: str, *, owner_id: Optional[str] = None) -> Optional[Job]:
        with self._lock:
            return self._visible(job_id, owner_id)

    def _filtered(self, status: Optional[JobStatus], owner_id: Optional[str]) -> List[Job]:
        jobs = [self._jobs[job_id] for job_id in reversed(self._order)]
        return [
            job
            for job in jobs
            if (status is None or job.status is status)
            and (owner_id is None or job.owner_id == owner_id)
        ]

    def list(
        self,
        *,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
        owner_id: Optional[str] = None,
    ) -> List[Job]:
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must not be negative")
        with self._lock:
            newest_first = self._filtered(status, owner_id)
        return newest_first[offset : offset + limit]

    def count(self, *, status: Optional[JobStatus] = None, owner_id: Optional[str] = None) -> int:
        with self._lock:
            return len(self._filtered(status, owner_id))

    def count_by_status(self, *, owner_id: Optional[str] = None) -> Dict[JobStatus, int]:
        with self._lock:
            tally = Counter(job.status for job in self._filtered(None, owner_id))
        return {status: tally.get(status, 0) for status in JobStatus}

    def update_where(
        self,
        job_id: str,
        changes: Mapping[str, Any],
        *,
        expected_status: StatusFilter,
        owner_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        attempts: Optional[int] = None,
    ) -> Optional[Job]:
        unknown = set(changes) - MUTABLE_JOB_FIELDS
        if unknown:
            raise ValueError(f"cannot change {sorted(unknown)}")
        with self._lock:
            current = self._visible(job_id, owner_id)
            if current is None or current.status not in _statuses(expected_status):
                return None
            if worker_id is not None and current.worker_id != worker_id:
                return None
            if attempts is not None and current.attempts != attempts:
                return None
            updated = current.model_copy(update={**changes, "updated_at": self._clock()})
            self._jobs[job_id] = updated
            return updated

    # --- worker --------------------------------------------------------------

    def claim(self, job_id: str, *, worker_id: str, lease_seconds: float) -> Optional[Job]:
        with self._lock:
            current = self._jobs.get(job_id)
            if (
                current is None
                or current.status is not JobStatus.QUEUED
                or current.cancel_requested_at is not None
                or current.attempts >= current.max_attempts
            ):
                return None
            now = self._clock()
            updated = current.model_copy(
                update={
                    "status": JobStatus.RUNNING,
                    "attempts": current.attempts + 1,
                    "worker_id": worker_id,
                    "started_at": current.started_at or now,
                    "heartbeat_at": now,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                    "completed_at": None,
                }
            )
            self._jobs[job_id] = updated
            return updated

    def heartbeat(
        self, job_id: str, *, worker_id: str, attempts: int, lease_seconds: float
    ) -> Optional[Job]:
        now = self._clock()
        return self.update_where(
            job_id,
            {"heartbeat_at": now, "lease_expires_at": now + timedelta(seconds=lease_seconds)},
            expected_status=JobStatus.RUNNING,
            worker_id=worker_id,
            attempts=attempts,
        )

    def find_stale(self, *, limit: int = 100) -> List[Job]:
        now = self._clock()
        with self._lock:
            stale = [
                job
                for job in self._jobs.values()
                if job.status is JobStatus.RUNNING
                and job.lease_expires_at is not None
                and job.lease_expires_at < now
            ]
        return sorted(stale, key=lambda job: job.lease_expires_at)[:limit]

    def find_orphaned(self, *, older_than_seconds: float, limit: int = 100) -> List[Job]:
        cutoff = self._clock() - timedelta(seconds=older_than_seconds)
        with self._lock:
            orphans = [
                job
                for job in self._jobs.values()
                if job.status is JobStatus.QUEUED and (job.updated_at or job.created_at) < cutoff
            ]
        return sorted(orphans, key=lambda job: job.created_at)[:limit]

    # --- targets -------------------------------------------------------------

    def list_targets(self, job_id: str, *, owner_id: Optional[str] = None) -> List[TargetRecord]:
        with self._lock:
            if self._visible(job_id, owner_id) is None:
                return []
            return list(self._targets.get(job_id, []))

    def update_target(self, job_id: str, position: int, changes: Mapping[str, Any]) -> None:
        with self._lock:
            targets = self._targets.get(job_id)
            if targets is None or not 0 <= position < len(targets):
                return
            targets[position] = targets[position].model_copy(update=dict(changes))

    # --- events --------------------------------------------------------------

    def add_event(
        self,
        job_id: str,
        kind: str,
        *,
        message: Optional[str] = None,
        attempt: Optional[int] = None,
        data: Optional[Mapping[str, Any]] = None,
        owner_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            if self._visible(job_id, owner_id) is None:
                return
            self._events.setdefault(job_id, []).append(
                JobEvent(
                    event_id=next(self._event_ids),
                    job_id=job_id,
                    kind=kind,
                    created_at=self._clock(),
                    attempt=attempt,
                    message=message,
                    data=dict(data or {}),
                )
            )

    def list_events(
        self, job_id: str, *, owner_id: Optional[str] = None, limit: int = 200
    ) -> List[JobEvent]:
        with self._lock:
            if self._visible(job_id, owner_id) is None:
                return []
            return list(self._events.get(job_id, []))[-limit:]

    # --- results -------------------------------------------------------------

    def upsert_result(self, result: ResultFile) -> ResultFile:
        with self._lock:
            existing = self._results.get((result.job_id, result.kind.value))
            if existing is not None:
                result = result.model_copy(update={"result_id": existing.result_id})
            self._results[(result.job_id, result.kind.value)] = result
            return result

    def list_results(self, job_id: str, *, owner_id: Optional[str] = None) -> List[ResultFile]:
        with self._lock:
            if self._visible(job_id, owner_id) is None:
                return []
            return sorted(
                (r for (jid, _), r in self._results.items() if jid == job_id),
                key=lambda r: r.kind.value,
            )

    def get_result(
        self, job_id: str, result_id: str, *, owner_id: Optional[str] = None
    ) -> Optional[ResultFile]:
        for result in self.list_results(job_id, owner_id=owner_id):
            if result.result_id == result_id:
                return result
        return None

