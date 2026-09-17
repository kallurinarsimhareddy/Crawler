"""How a newly created job reaches a worker.

This is the seam where the job queue goes. The API calls
:meth:`JobDispatcher.dispatch` after creating a job and does not care what
happens next:

* :class:`NullDispatcher` does nothing. The job stays ``queued`` until something
  else picks it up — which, in Phase 5B, is a separate worker process reading
  Redis, and the API's dispatcher becomes one that enqueues the id.
* :class:`InlineDispatcher` runs the job on a small thread pool inside the API
  process. Local development only: it dies with the process and cannot scale
  past one machine, but it lets the dashboard show a job move end to end today.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import List, Optional

from cloud.worker.executor import JobExecutor

__all__ = ["InlineDispatcher", "JobDispatcher", "NullDispatcher"]


class JobDispatcher(ABC):
    """Hands queued jobs to whatever will run them."""

    #: Reported by the health endpoint.
    name: str = "dispatcher"

    @abstractmethod
    def dispatch(self, job_id: str) -> None:
        """Arrange for the job to run. Must return promptly."""

    def shutdown(self) -> None:
        """Release resources. Called once when the API stops."""


class NullDispatcher(JobDispatcher):
    """Leaves jobs queued."""

    name = "none"

    def dispatch(self, job_id: str) -> None:
        return None


class InlineDispatcher(JobDispatcher):
    """Runs jobs on background threads in this process."""

    def __init__(self, executor: JobExecutor, *, max_concurrent: int = 2, name: str = "inline") -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self.name = name
        self._executor = executor
        self._pool = ThreadPoolExecutor(
            max_workers=max_concurrent, thread_name_prefix="careercloud-job"
        )
        self._lock = threading.Lock()
        self._pending: List[Future] = []

    def dispatch(self, job_id: str) -> None:
        future = self._pool.submit(self._executor.execute, job_id)
        with self._lock:
            self._pending = [f for f in self._pending if not f.done()]
            self._pending.append(future)

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Block until every dispatched job has finished. For tests."""
        with self._lock:
            pending = list(self._pending)
        _, not_done = wait(pending, timeout=timeout)
        return not not_done

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
