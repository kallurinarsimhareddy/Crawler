"""A runner that pretends to crawl.

It walks a job's targets one "step" at a time, reporting progress and honouring
cancellation exactly as a real runner must, but it opens no connection, starts
no browser and writes no file. That makes it safe to wire into a local API for
the dashboard to watch, and deterministic enough to test the lifecycle with.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from cloud.shared.models import Job, TargetStatus
from cloud.worker.runner import JobRunner, RunContext, RunResult

__all__ = ["FakeRunner"]


class FakeRunner(JobRunner):
    """Simulates a crawl.

    Args:
        step_seconds: Pause between steps, so a person watching the dashboard
            can see a job move. ``0`` for tests.
        steps: Steps for a job with no targets (a weekly crawl). A job with
            targets takes one step per target.
        fail_with: If set, the run fails with this message after its first step.
        raise_error: If set, the run raises this exception after its first step,
            to exercise the executor's handling of a runner that blows up.
        sleep: Injected for tests.
    """

    name = "fake"

    def __init__(
        self,
        *,
        step_seconds: float = 0.0,
        steps: int = 3,
        fail_with: Optional[str] = None,
        raise_error: Optional[BaseException] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if step_seconds < 0:
            raise ValueError("step_seconds must not be negative")
        if steps < 1:
            raise ValueError("steps must be at least 1")
        self._step_seconds = step_seconds
        self._steps = steps
        self._fail_with = fail_with
        self._raise_error = raise_error
        self._sleep = sleep

    def run(self, job: Job, context: RunContext) -> RunResult:
        labels = [target.label() for target in job.targets] or [
            f"step {index + 1}" for index in range(self._steps)
        ]
        total = len(labels)

        # The richer context calls exist on the executor's context; a bare
        # Phase 5A context (report_progress/is_cancelled only) still works.
        update = getattr(context, "update", None)
        started = getattr(context, "target_started", None)
        finished = getattr(context, "target_finished", None)
        has_targets = bool(job.targets)

        for index, label in enumerate(labels):
            if context.is_cancelled():
                return RunResult.cancelled()
            if update is not None:
                update(current_phase="crawling", current_company=label)
            if started is not None and has_targets:
                started(index)
            context.report_progress(index, total, f"Crawling {label}")
            if self._step_seconds:
                self._sleep(self._step_seconds)

            if index == 0:
                if self._raise_error is not None:
                    raise self._raise_error
                if self._fail_with is not None:
                    return RunResult.failed(self._fail_with)
            if finished is not None and has_targets:
                finished(index, status=TargetStatus.COMPLETED, platform="Simulated", outcome="no open jobs", jobs_found=0)

        if context.is_cancelled():
            return RunResult.cancelled()
        context.report_progress(total, total, "Finished (simulated)")
        return RunResult.completed(companies=total, jobs_found=0, simulated=True)
