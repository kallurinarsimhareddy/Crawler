"""The rules of a job's life, in one place.

:class:`JobService` is the only thing that creates jobs or changes their status.
The API calls it for requests; the worker calls it as a run progresses. Neither
writes to the repository directly, so the transition table in
:mod:`cloud.shared.models` cannot be bypassed.

There are three kinds of caller, and the methods are grouped by them:

* **Users** (through the API) pass ``owner_id`` and can only create, read and
  cancel their own jobs.
* **Workers** pass the :class:`~cloud.shared.models.Job` they claimed. Every
  write is *fenced* on that claim's ``worker_id`` and ``attempts``: once a job
  has been reaped and handed to someone else, the old worker's writes simply do
  not land.
* **Phase 5A callers** (``start_job``, ``complete_job``…) are unfenced and
  unscoped. They remain for the in-process executor and its tests.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from cloud.shared.models import (
    Job,
    JobEvent,
    JobProgress,
    JobStatus,
    JobType,
    ResultFile,
    TargetRecord,
    can_transition,
)
from cloud.shared.repository import JobRepository
from cloud.shared.schemas import JobCreateRequest, request_targets

__all__ = [
    "InvalidTransitionError",
    "JobNotFoundError",
    "JobService",
    "ReapAction",
    "ReapOutcome",
    "RetryPolicy",
]

log = logging.getLogger(__name__)

#: Longest error message stored on a job. A crawler traceback can be enormous;
#: the job record is a status, not a log.
MAX_ERROR_LENGTH = 2000


class JobNotFoundError(LookupError):
    """No job has this id — or none the caller is allowed to see."""

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


@dataclass(frozen=True)
class RetryPolicy:
    """How often, and how patiently, a job is retried.

    ``max_attempts`` counts *claims*, including the first, so ``3`` means one
    run and at most two retries. The delay doubles per attempt and is capped.
    There is no unbounded path: an attempt that is not retried fails the job.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 30.0
    max_delay_seconds: float = 900.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must not be negative")

    def delay_after(self, attempt: int) -> float:
        """Seconds to wait before the attempt after ``attempt``."""
        return min(self.max_delay_seconds, self.base_delay_seconds * (2 ** max(0, attempt - 1)))


class ReapAction:
    REQUEUED = "requeued"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ReapOutcome:
    job: Job
    action: str
    delay_seconds: float = 0.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


def _clip_error(error: Optional[str]) -> str:
    text = (error or "").strip() or "unknown error"
    if len(text) > MAX_ERROR_LENGTH:
        text = text[: MAX_ERROR_LENGTH - 1] + "…"
    return text


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

    @property
    def repository(self) -> JobRepository:
        return self._repository

    def now(self) -> datetime:
        return self._clock()

    # --- reads ---------------------------------------------------------------

    def get_job(self, job_id: str, *, owner_id: Optional[str] = None) -> Job:
        job = self._repository.get(job_id, owner_id=owner_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def list_jobs(
        self,
        *,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
        owner_id: Optional[str] = None,
    ) -> List[Job]:
        return self._repository.list(status=status, limit=limit, offset=offset, owner_id=owner_id)

    def count_jobs(self, *, status: Optional[JobStatus] = None, owner_id: Optional[str] = None) -> int:
        return self._repository.count(status=status, owner_id=owner_id)

    def counts_by_status(self, *, owner_id: Optional[str] = None) -> Dict[JobStatus, int]:
        return self._repository.count_by_status(owner_id=owner_id)

    def list_targets(self, job_id: str, *, owner_id: Optional[str] = None) -> List[TargetRecord]:
        self.get_job(job_id, owner_id=owner_id)
        return self._repository.list_targets(job_id, owner_id=owner_id)

    def list_events(self, job_id: str, *, owner_id: Optional[str] = None) -> List[JobEvent]:
        self.get_job(job_id, owner_id=owner_id)
        return self._repository.list_events(job_id, owner_id=owner_id)

    def list_results(self, job_id: str, *, owner_id: Optional[str] = None) -> List[ResultFile]:
        self.get_job(job_id, owner_id=owner_id)
        return self._repository.list_results(job_id, owner_id=owner_id)

    def get_result(
        self, job_id: str, result_id: str, *, owner_id: Optional[str] = None
    ) -> Optional[ResultFile]:
        self.get_job(job_id, owner_id=owner_id)
        return self._repository.get_result(job_id, result_id, owner_id=owner_id)

    def active_job_count(self, *, owner_id: str) -> int:
        counts = self.counts_by_status(owner_id=owner_id)
        return counts[JobStatus.QUEUED] + counts[JobStatus.RUNNING]

    # --- creation ------------------------------------------------------------

    def create_job(
        self,
        request: JobCreateRequest,
        *,
        owner_id: Optional[str] = None,
        max_attempts: int = 1,
        unrunnable_reason: Optional[str] = None,
    ) -> Job:
        """Record a validated request as a queued job, owned by ``owner_id``.

        ``unrunnable_reason`` marks a job no runner in this deployment will pick
        up (e.g. ``weekly_crawl``). It is still recorded — and can be cancelled —
        but it says plainly that it will not start.
        """
        now = self._clock()
        targets = request_targets(request)
        progress = JobProgress(total=len(targets) or None, current_phase="queued")
        if unrunnable_reason:
            progress = progress.model_copy(
                update={"current_phase": "unsupported", "message": unrunnable_reason}
            )
        job = Job(
            job_id=self._id_factory(),
            type=JobType(request.type),
            status=JobStatus.QUEUED,
            targets=targets,
            target_count=len(targets),
            created_at=now,
            updated_at=now,
            owner_id=owner_id,
            max_attempts=max_attempts,
            progress=progress,
        )
        self._repository.add(job, owner_id=owner_id)
        self._repository.add_event(job.job_id, "created", owner_id=owner_id)
        return job

    # --- user actions --------------------------------------------------------

    def request_cancel(self, job_id: str, *, owner_id: Optional[str]) -> Job:
        """Ask a job to stop.

        A queued job is cancelled on the spot. A running job is marked
        ``cancel_requested`` and stays running until its worker notices — at
        its next heartbeat or between companies — and stops. Asking twice is
        harmless. A finished job cannot be cancelled.
        """
        for _ in range(3):  # the job can move under us; re-read and try again
            current = self.get_job(job_id, owner_id=owner_id)
            now = self._clock()

            if current.status is JobStatus.QUEUED:
                progress = current.progress.model_copy(
                    update={"current_phase": "cancelled", "current_company": None}
                )
                updated = self._repository.update_where(
                    job_id,
                    {
                        "status": JobStatus.CANCELLED,
                        "completed_at": now,
                        "cancel_requested_at": now,
                        "progress": progress,
                    },
                    expected_status=JobStatus.QUEUED,
                    owner_id=owner_id,
                )
                if updated is not None:
                    self._repository.add_event(job_id, "cancelled", owner_id=owner_id)
                    return updated
                continue

            if current.status is JobStatus.RUNNING:
                if current.cancel_requested_at is not None:
                    return current
                updated = self._repository.update_where(
                    job_id,
                    {"cancel_requested_at": now},
                    expected_status=JobStatus.RUNNING,
                    owner_id=owner_id,
                )
                if updated is not None:
                    self._repository.add_event(job_id, "cancel_requested", owner_id=owner_id)
                    return updated
                continue

            raise InvalidTransitionError(job_id, current.status, JobStatus.CANCELLED)

        latest = self.get_job(job_id, owner_id=owner_id)
        raise InvalidTransitionError(job_id, latest.status, JobStatus.CANCELLED)

    # --- worker actions (fenced) ---------------------------------------------

    def claim_job(self, job_id: str, *, worker_id: str, lease_seconds: float) -> Optional[Job]:
        """queued -> running for this worker, or ``None`` if it is not claimable."""
        claimed = self._repository.claim(job_id, worker_id=worker_id, lease_seconds=lease_seconds)
        if claimed is None:
            return None
        progress = claimed.progress.model_copy(
            update={"current_phase": "starting", "message": "Starting"}
        )
        updated = self._fenced(claimed, {"progress": progress}) or claimed
        self._repository.add_event(
            job_id, "claimed", attempt=claimed.attempts, data={"worker_id": worker_id}
        )
        return updated

    def heartbeat(self, claimed: Job, *, lease_seconds: float) -> Optional[Job]:
        """Extend the lease. ``None`` means this worker no longer holds the job."""
        return self._repository.heartbeat(
            claimed.job_id,
            worker_id=claimed.worker_id or "",
            attempts=claimed.attempts,
            lease_seconds=lease_seconds,
        )

    def update_progress(self, claimed: Job, **fields: Any) -> Optional[Job]:
        """Merge ``fields`` into the job's progress. ``None`` if the lease is lost."""
        current = self._repository.get(claimed.job_id)
        if current is None:
            return None
        progress = current.progress.model_copy(update=fields)
        return self._fenced(claimed, {"progress": progress})

    def update_target(self, claimed: Job, position: int, **changes: Any) -> None:
        self._repository.update_target(claimed.job_id, position, changes)

    def record_result(self, result: ResultFile) -> ResultFile:
        return self._repository.upsert_result(result)

    def record_event(self, job_id: str, kind: str, **kwargs: Any) -> None:
        self._repository.add_event(job_id, kind, **kwargs)

    def finish_completed(self, claimed: Job, *, message: str = "Completed") -> Optional[Job]:
        current = self._repository.get(claimed.job_id)
        if current is None:
            return None
        progress = current.progress.model_copy(
            update={
                "current_phase": "completed",
                "current_company": None,
                "message": message,
                "completed": current.progress.total
                if current.progress.total is not None
                else current.progress.completed,
            }
        )
        done = self._fenced(
            claimed,
            {
                "status": JobStatus.COMPLETED,
                "completed_at": self._clock(),
                "progress": progress,
                "lease_expires_at": None,
                "error": None,
            },
        )
        if done is not None:
            self._repository.add_event(claimed.job_id, "completed", attempt=claimed.attempts)
        return done

    def finish_failed(self, claimed: Job, error: str) -> Optional[Job]:
        text = _clip_error(error)
        current = self._repository.get(claimed.job_id)
        progress = (current or claimed).progress.model_copy(
            update={"current_phase": "failed", "current_company": None}
        )
        done = self._fenced(
            claimed,
            {
                "status": JobStatus.FAILED,
                "completed_at": self._clock(),
                "error": text,
                "progress": progress,
                "lease_expires_at": None,
            },
        )
        if done is not None:
            self._repository.add_event(
                claimed.job_id, "failed", attempt=claimed.attempts, message=text
            )
        return done

    def finish_cancelled(self, claimed: Job) -> Optional[Job]:
        current = self._repository.get(claimed.job_id)
        progress = (current or claimed).progress.model_copy(
            update={"current_phase": "cancelled", "current_company": None}
        )
        done = self._fenced(
            claimed,
            {
                "status": JobStatus.CANCELLED,
                "completed_at": self._clock(),
                "progress": progress,
                "lease_expires_at": None,
            },
        )
        if done is not None:
            self._repository.add_event(claimed.job_id, "cancelled", attempt=claimed.attempts)
        return done

    def retry_or_fail(
        self, claimed: Job, error: str, policy: RetryPolicy
    ) -> Tuple[Optional[Job], Optional[float]]:
        """After a retryable failure: requeue if attempts remain, else fail.

        Returns ``(job, delay)``. ``delay`` is set only when the job was
        requeued and must be re-enqueued after that many seconds. ``job`` is
        ``None`` when the lease was already lost.
        """
        text = _clip_error(error)
        limit = min(claimed.max_attempts, policy.max_attempts)
        if claimed.attempts >= limit:
            if limit > 1:
                text = f"{text} (gave up after {claimed.attempts} attempts)"
            return self.finish_failed(claimed, text), None

        delay = policy.delay_after(claimed.attempts)
        current = self._repository.get(claimed.job_id)
        progress = (current or claimed).progress.model_copy(
            update={"current_phase": "retry_scheduled", "current_company": None}
        )
        requeued = self._fenced(
            claimed,
            {
                "status": JobStatus.QUEUED,
                "error": text,
                "worker_id": None,
                "lease_expires_at": None,
                "progress": progress,
            },
        )
        if requeued is None:
            return None, None
        self._repository.add_event(
            claimed.job_id,
            "retry_scheduled",
            attempt=claimed.attempts,
            message=text,
            data={"delay_seconds": delay},
        )
        return requeued, delay

    def release_for_shutdown(self, claimed: Job) -> Optional[Job]:
        """Give a job back because this worker is stopping, refunding the attempt."""
        current = self._repository.get(claimed.job_id)
        progress = (current or claimed).progress.model_copy(
            update={"current_phase": "queued", "current_company": None, "message": "Worker restarting"}
        )
        released = self._fenced(
            claimed,
            {
                "status": JobStatus.QUEUED,
                "attempts": max(0, claimed.attempts - 1),
                "worker_id": None,
                "lease_expires_at": None,
                "progress": progress,
            },
        )
        if released is not None:
            self._repository.add_event(claimed.job_id, "released_on_shutdown", attempt=claimed.attempts)
        return released

    # --- recovery ------------------------------------------------------------

    def reap_stale(self, policy: RetryPolicy, *, limit: int = 100) -> List[ReapOutcome]:
        """Deal with running jobs whose worker stopped heartbeating.

        Each is cancelled if cancellation was requested, failed if it has used
        its attempts, and otherwise requeued. Every write is conditional on the
        job still being on the stale attempt, so two reapers — or a reaper and a
        worker that was merely slow — cannot both act on it.
        """
        outcomes: List[ReapOutcome] = []
        for stale in self._repository.find_stale(limit=limit):
            fence = {"worker_id": stale.worker_id, "attempts": stale.attempts}
            now = self._clock()
            base = stale.progress.model_copy(update={"current_company": None})

            if stale.cancel_requested_at is not None:
                changes: Dict[str, Any] = {
                    "status": JobStatus.CANCELLED,
                    "completed_at": now,
                    "lease_expires_at": None,
                    "progress": base.model_copy(update={"current_phase": "cancelled"}),
                }
                action, delay = ReapAction.CANCELLED, 0.0
            elif stale.attempts >= min(stale.max_attempts, policy.max_attempts):
                changes = {
                    "status": JobStatus.FAILED,
                    "completed_at": now,
                    "lease_expires_at": None,
                    "error": f"worker stopped responding (attempt {stale.attempts} of {stale.max_attempts})",
                    "progress": base.model_copy(update={"current_phase": "failed"}),
                }
                action, delay = ReapAction.FAILED, 0.0
            else:
                changes = {
                    "status": JobStatus.QUEUED,
                    "worker_id": None,
                    "lease_expires_at": None,
                    "error": "worker stopped responding; requeued",
                    "progress": base.model_copy(update={"current_phase": "requeued"}),
                }
                action, delay = ReapAction.REQUEUED, policy.delay_after(stale.attempts)

            updated = self._repository.update_where(
                stale.job_id, changes, expected_status=JobStatus.RUNNING, **fence
            )
            if updated is None:
                continue
            self._repository.add_event(
                stale.job_id,
                f"reaped_{action}",
                attempt=stale.attempts,
                data={"worker_id": stale.worker_id},
            )
            log.warning("reaped stale job %s (%s)", stale.job_id, action)
            outcomes.append(ReapOutcome(updated, action, delay))
        return outcomes

    def find_orphaned(self, *, older_than_seconds: float, limit: int = 100) -> List[Job]:
        return self._repository.find_orphaned(older_than_seconds=older_than_seconds, limit=limit)

    def touch(self, job: Job) -> Optional[Job]:
        """Refresh a queued job's ``updated_at`` after re-enqueueing it."""
        return self._repository.update_where(job.job_id, {}, expected_status=JobStatus.QUEUED)

    # --- Phase 5A lifecycle (unfenced) ---------------------------------------

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
            progress = progress.model_copy(
                update={
                    "completed": progress.total if progress.total is not None else progress.completed,
                    "message": message if message is not None else progress.message,
                }
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
        return self._transition(
            job_id, JobStatus.FAILED, completed_at=self._clock(), error=_clip_error(error)
        )

    def cancel_job(self, job_id: str) -> Job:
        """queued or running -> cancelled, immediately."""
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
        progress = current.progress.model_copy(
            update={"completed": completed, "total": total, "message": message}
        )
        updated = self._repository.update_where(
            job_id, {"progress": progress}, expected_status=JobStatus.RUNNING
        )
        if updated is None:
            latest = self.get_job(job_id)
            raise InvalidTransitionError(job_id, latest.status, JobStatus.RUNNING)
        return updated

    def is_cancelled(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return job.status is JobStatus.CANCELLED or job.cancel_requested_at is not None

    # --- internals -----------------------------------------------------------

    def _fenced(self, claimed: Job, changes: Dict[str, Any]) -> Optional[Job]:
        return self._repository.update_where(
            claimed.job_id,
            changes,
            expected_status=JobStatus.RUNNING,
            worker_id=claimed.worker_id,
            attempts=claimed.attempts,
        )

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
        updated = self._repository.update_where(
            job_id, {"status": new_status, **changes}, expected_status=current.status
        )
        if updated is None:
            # Someone else moved the job between our read and our write — a
            # cancel racing a finishing worker, typically. Report what it is now.
            latest = self.get_job(job_id)
            raise InvalidTransitionError(job_id, latest.status, new_status)
        return updated
