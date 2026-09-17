"""Run one job and record how it went.

:class:`JobExecutor` is the body of a worker's loop: claim a job, keep its lease
alive, hand it to a :class:`~cloud.worker.runner.JobRunner`, store its results,
and record the outcome. :class:`~cloud.worker.worker.Worker` calls it for each
queue delivery; :class:`~cloud.worker.dispatcher.InlineDispatcher` calls it
in-process for local development.

The rules it enforces:

* **Only a claimed job runs.** The claim is an atomic ``queued -> running``
  update. A duplicate delivery, a cancelled job or a job another worker holds
  is returned untouched — which is what makes execution idempotent.
* **Every write is fenced** on this worker's id and the claimed attempt. If the
  lease is lost (the reaper decided this worker was dead and requeued the job),
  the heartbeat notices, the runner is told to stop, and nothing further is
  written.
* **Whatever the runner does, the job ends somewhere definite**: completed,
  failed, cancelled, or back in the queue for a bounded retry.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from cloud.shared.models import Job, JobStatus, TargetStatus
from cloud.shared.service import JobNotFoundError, JobService, RetryPolicy
from cloud.worker.results import ResultWriter
from cloud.worker.runner import JobRunner, RunOutcome, RunResult
from cloud.worker.workspace import JobWorkspace

__all__ = ["JobExecutor", "default_worker_id"]

log = logging.getLogger(__name__)


def default_worker_id() -> str:
    import os
    import socket

    return f"{socket.gethostname()[:40]}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class _Heartbeat:
    """Extends the job's lease until stopped; flags the job lost if it cannot."""

    def __init__(
        self,
        service: JobService,
        claimed: Job,
        *,
        lease_seconds: float,
        interval: float,
        lost: threading.Event,
        on_beat: Optional[Callable[[], None]],
    ) -> None:
        self._service = service
        self._claimed = claimed
        self._lease = lease_seconds
        self._interval = interval
        self._lost = lost
        self._on_beat = on_beat
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{claimed.job_id[-8:]}", daemon=True
        )

    def start(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                beat = self._service.heartbeat(self._claimed, lease_seconds=self._lease)
            except Exception:  # a database blip: try again next interval
                log.warning("heartbeat for %s failed", self._claimed.job_id, exc_info=True)
                continue
            if beat is None:
                log.warning("lost the lease on %s; stopping", self._claimed.job_id)
                self._lost.set()
                return
            if self._on_beat is not None:
                try:
                    self._on_beat()
                except Exception:
                    log.warning("queue visibility extension failed", exc_info=True)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval + 5)


class _ExecutionContext:
    """The :class:`~cloud.worker.runner.RunContext` a runner receives."""

    def __init__(
        self,
        service: JobService,
        claimed: Job,
        workspace: Optional[JobWorkspace],
        *,
        lost: threading.Event,
        stopping: Optional[threading.Event],
        cancel_check_interval: float,
    ) -> None:
        self._service = service
        self._claimed = claimed
        self.workspace = workspace
        self._lost = lost
        self._stopping = stopping
        self._interval = cancel_check_interval
        self._checked_at = 0.0
        self._cancel_requested = False
        self._lock = threading.Lock()
        self.stopped_for_shutdown = False

    def _write(self, **fields: Any) -> None:
        if self._lost.is_set():
            return
        try:
            updated = self._service.update_progress(self._claimed, **fields)
        except Exception:
            log.warning("progress update for %s failed", self._claimed.job_id, exc_info=True)
            return
        if updated is None:
            self._lost.set()  # cancelled outright, or reassigned
        elif updated.cancel_requested_at is not None:
            self._cancel_requested = True

    def report_progress(
        self, completed: int, total: Optional[int] = None, message: Optional[str] = None
    ) -> None:
        self._write(completed=completed, total=total, message=message)

    def update(self, **progress: Any) -> None:
        self._write(**progress)

    def target_started(self, position: int) -> None:
        try:
            self._service.update_target(
                self._claimed, position, status=TargetStatus.RUNNING, started_at=self._service.now()
            )
        except Exception:
            log.warning("target update failed", exc_info=True)

    def target_finished(self, position: int, **outcome: Any) -> None:
        try:
            self._service.update_target(
                self._claimed, position, completed_at=self._service.now(), **outcome
            )
        except Exception:
            log.warning("target update failed", exc_info=True)

    def is_cancelled(self) -> bool:
        if self._stopping is not None and self._stopping.is_set():
            self.stopped_for_shutdown = True
            return True
        if self._lost.is_set() or self._cancel_requested:
            return True
        with self._lock:
            now = time.monotonic()
            if self._checked_at and now - self._checked_at < self._interval:
                return False
            self._checked_at = now
        try:
            current = self._service.repository.get(self._claimed.job_id)
        except Exception:
            log.warning("cancellation check failed", exc_info=True)
            return False
        if (
            current is None
            or current.status is not JobStatus.RUNNING
            or current.worker_id != self._claimed.worker_id
            or current.attempts != self._claimed.attempts
        ):
            self._lost.set()
            return True
        if current.cancel_requested_at is not None:
            self._cancel_requested = True
            return True
        return False


class JobExecutor:
    """Drives one job from ``queued`` to wherever it ends.

    Args:
        service: The job service.
        runner: What does the work.
        worker_id: Identifies this worker in leases. Generated if omitted.
        lease_seconds: How long a claim stays valid without a heartbeat.
        heartbeat_interval: Seconds between heartbeats. Defaults to a third of
            the lease, so two missed beats still leave the lease intact.
        retry_policy: Bounds retries of retryable failures.
        result_writer: Stores result files. ``None`` keeps none (Phase 5A).
        runtime_root: Where per-attempt workspaces are created. ``None`` gives
            the runner no workspace.
        keep_workspaces: Leave workspaces on disk after the attempt, for debugging.
        on_requeue: Called with ``(job_id, delay_seconds)`` after a retry or a
            shutdown release puts the job back in ``queued``.
        stopping: Set when the worker is shutting down.
        cancel_check_interval: Minimum seconds between database cancellation checks.
    """

    def __init__(
        self,
        service: JobService,
        runner: JobRunner,
        *,
        worker_id: Optional[str] = None,
        lease_seconds: float = 120.0,
        heartbeat_interval: Optional[float] = None,
        retry_policy: Optional[RetryPolicy] = None,
        result_writer: Optional[ResultWriter] = None,
        runtime_root: Optional[Path] = None,
        keep_workspaces: bool = False,
        on_requeue: Optional[Callable[[str, float], None]] = None,
        stopping: Optional[threading.Event] = None,
        cancel_check_interval: float = 1.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._service = service
        self._runner = runner
        self.worker_id = worker_id or default_worker_id()
        self._lease = lease_seconds
        self._interval = heartbeat_interval if heartbeat_interval is not None else lease_seconds / 3
        self._policy = retry_policy or RetryPolicy()
        self._writer = result_writer
        self._runtime_root = runtime_root
        self._keep = keep_workspaces
        self._on_requeue = on_requeue
        self._stopping = stopping
        self._cancel_interval = cancel_check_interval

    @property
    def runner(self) -> JobRunner:
        return self._runner

    @property
    def runtime_root(self) -> Optional[Path]:
        return self._runtime_root

    def execute(self, job_id: str, *, on_heartbeat: Optional[Callable[[], None]] = None) -> Job:
        """Run the job if it can be claimed, and return it as it ended up.

        A job that is not claimable — cancelled before a worker reached it,
        already held by another worker, finished, out of attempts, or of a type
        this runner does not execute — is returned untouched.
        """
        queued = self._service.get_job(job_id)
        if not self._runner.supports(queued.type):
            log.info("not running %s: %s runner does not execute %s jobs", job_id, self._runner.name, queued.type.value)
            return queued

        claimed = self._service.claim_job(job_id, worker_id=self.worker_id, lease_seconds=self._lease)
        if claimed is None:
            current = self._service.get_job(job_id)
            log.info("not running %s: it is %s", job_id, current.status.value)
            return current

        lost = threading.Event()
        heartbeat = _Heartbeat(
            self._service,
            claimed,
            lease_seconds=self._lease,
            interval=self._interval,
            lost=lost,
            on_beat=on_heartbeat,
        ).start()
        workspace: Optional[JobWorkspace] = None
        try:
            if self._runtime_root is not None:
                workspace = JobWorkspace.create(self._runtime_root, job_id, claimed.attempts)
            context = _ExecutionContext(
                self._service,
                claimed,
                workspace,
                lost=lost,
                stopping=self._stopping,
                cancel_check_interval=self._cancel_interval,
            )
            try:
                result = self._runner.run(claimed, context)
            except Exception as error:  # the runner's failure is the job's failure
                log.exception("runner %s raised on %s", self._runner.name, job_id)
                result = RunResult.failed(f"{type(error).__name__}: {error}", retryable=True)

            heartbeat.stop()
            if lost.is_set():
                log.warning("%s: lease lost during the run; recording nothing", job_id)
                return self._current(job_id)
            if context.stopped_for_shutdown and result.outcome is not RunOutcome.COMPLETED:
                return self._release(claimed)
            return self._record(claimed, result, workspace)
        finally:
            heartbeat.stop()
            if workspace is not None and not self._keep:
                try:
                    workspace.remove()
                except Exception:
                    log.warning("could not remove workspace %s", workspace.path, exc_info=True)

    # --- outcomes ------------------------------------------------------------

    def _current(self, job_id: str) -> Job:
        try:
            return self._service.get_job(job_id)
        except JobNotFoundError:
            raise

    def _requeued(self, job_id: str, delay: float) -> None:
        if self._on_requeue is not None:
            try:
                self._on_requeue(job_id, delay)
            except Exception:
                # The reaper's orphan sweep will enqueue it again.
                log.warning("could not re-enqueue %s", job_id, exc_info=True)

    def _release(self, claimed: Job) -> Job:
        released = self._service.release_for_shutdown(claimed)
        if released is not None:
            self._requeued(claimed.job_id, 0.0)
        return self._current(claimed.job_id)

    def _retry_or_fail(self, claimed: Job, error: str) -> Job:
        job, delay = self._service.retry_or_fail(claimed, error, self._policy)
        if job is not None and delay is not None:
            log.info("%s: attempt %s failed; retrying in %.0fs", claimed.job_id, claimed.attempts, delay)
            self._requeued(claimed.job_id, delay)
        return self._current(claimed.job_id)

    def _record(self, claimed: Job, result: RunResult, workspace: Optional[JobWorkspace]) -> Job:
        job_id = claimed.job_id

        if result.outcome is RunOutcome.CANCELLED:
            self._service.finish_cancelled(claimed)
            return self._current(job_id)

        if result.outcome is RunOutcome.FAILED:
            if result.retryable:
                return self._retry_or_fail(claimed, result.error or "unknown error")
            self._service.finish_failed(claimed, result.error or "unknown error")
            return self._current(job_id)

        if self._writer is not None and workspace is not None:
            try:
                self._service.update_progress(claimed, current_phase="saving_results", current_company=None)
                for stored in self._writer.write(claimed, result, workspace):
                    self._service.record_result(stored)
            except Exception as error:
                log.exception("%s: storing results failed", job_id)
                return self._retry_or_fail(claimed, f"could not store results: {type(error).__name__}: {error}")

        self._service.finish_completed(claimed, message="Completed")
        return self._current(job_id)
