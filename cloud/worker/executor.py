"""Run one job and record how it went.

:class:`JobExecutor` is the body of a worker's loop: take a job id, start it,
hand it to a :class:`~cloud.worker.runner.JobRunner`, and record the outcome.
Phase 5B wraps it in a queue consumer; Phase 5A calls it from
:class:`~cloud.worker.dispatcher.InlineDispatcher` inside the API process.

The rule it enforces: **whatever the runner does, the job ends in a terminal
status.** A runner that returns, fails, raises or is cancelled mid-flight never
leaves a job stuck in ``running``.
"""

from __future__ import annotations

import logging
from typing import Optional

from cloud.shared.models import Job, JobStatus
from cloud.shared.service import InvalidTransitionError, JobService
from cloud.worker.runner import JobRunner, RunOutcome, RunResult

__all__ = ["JobExecutor"]

log = logging.getLogger(__name__)


class _ServiceRunContext:
    """A :class:`~cloud.worker.runner.RunContext` backed by the job service."""

    def __init__(self, service: JobService, job_id: str) -> None:
        self._service = service
        self._job_id = job_id

    def report_progress(
        self, completed: int, total: Optional[int] = None, message: Optional[str] = None
    ) -> None:
        try:
            self._service.report_progress(
                self._job_id, completed=completed, total=total, message=message
            )
        except InvalidTransitionError:
            # The job left "running" under us — cancelled, most likely. The
            # runner finds out through is_cancelled(); progress is not worth a crash.
            pass

    def is_cancelled(self) -> bool:
        return self._service.is_cancelled(self._job_id)


class JobExecutor:
    """Drives one job from ``queued`` to a terminal status."""

    def __init__(self, service: JobService, runner: JobRunner) -> None:
        self._service = service
        self._runner = runner

    def execute(self, job_id: str) -> Job:
        """Run the job and return it as it ended.

        A job that is no longer ``queued`` — cancelled before a worker reached
        it, or already claimed by another worker — is returned untouched.
        """
        queued = self._service.get_job(job_id)
        try:
            job = self._service.start_job(job_id, total=len(queued.targets) or None)
        except InvalidTransitionError as refused:
            log.info("not running %s: it is already %s", job_id, refused.current.value)
            return self._service.get_job(job_id)

        context = _ServiceRunContext(self._service, job_id)
        try:
            result = self._runner.run(job, context)
        except Exception as error:  # the runner's failure is the job's failure
            log.exception("runner %s raised on %s", self._runner.name, job_id)
            result = RunResult.failed(f"{type(error).__name__}: {error}")

        return self._record(job_id, result)

    def _record(self, job_id: str, result: RunResult) -> Job:
        try:
            if result.outcome is RunOutcome.COMPLETED:
                return self._service.complete_job(job_id, message="Completed")
            if result.outcome is RunOutcome.FAILED:
                return self._service.fail_job(job_id, result.error or "unknown error")
            return self._service.cancel_job(job_id)
        except InvalidTransitionError as refused:
            # Cancelled while the runner was finishing: the cancel stands.
            if refused.current is not JobStatus.CANCELLED:
                log.warning("could not record %s for %s: %s", result.outcome.value, job_id, refused)
            return self._service.get_job(job_id)
