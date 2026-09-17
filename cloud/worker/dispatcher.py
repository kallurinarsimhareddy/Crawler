"""How a newly created job reaches a worker.

The API calls :meth:`JobDispatcher.dispatch` after the job row is committed and
does not care what happens next:

* :class:`QueueDispatcher` enqueues the id on a :class:`~cloud.shared.queue.JobQueue`
  (Redis in production). A separate worker process picks it up.
* :class:`NullDispatcher` does nothing. The job stays ``queued``; the worker's
  orphan sweep will still find it.
* :class:`InlineDispatcher` runs the job on a small thread pool inside the API
  process. Local development only: it dies with the process and cannot scale
  past one machine, but it needs no Redis and no worker.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import List, Optional

from cloud.shared.queue import JobQueue
from cloud.worker.executor import JobExecutor

__all__ = ["InlineDispatcher", "JobDispatcher", "NullDispatcher", "QueueDispatcher"]

log = logging.getLogger(__name__)


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


class QueueDispatcher(JobDispatcher):
    """Enqueues the job id for an out-of-process worker."""

    def __init__(self, queue: JobQueue) -> None:
        self._queue = queue
        self.name = queue.name

    def dispatch(self, job_id: str) -> None:
        # A failure here is logged, not raised: the job row already exists and
        # the worker's orphan sweep enqueues any queued job left untouched.
        try:
            self._queue.enqueue(job_id)
        except Exception:
            log.exception("could not enqueue %s; the orphan sweep will retry", job_id)

    def shutdown(self) -> None:
        self._queue.close()


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
        self._timers: List[threading.Timer] = []
        self._closed = False

    def dispatch(self, job_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            future = self._pool.submit(self._executor.execute, job_id)
            self._pending = [f for f in self._pending if not f.done()]
            self._pending.append(future)

    def dispatch_later(self, job_id: str, delay_seconds: float) -> None:
        """Used as the executor's ``on_requeue`` so retries run in-process too."""
        if delay_seconds <= 0:
            self.dispatch(job_id)
            return
        timer = threading.Timer(delay_seconds, self.dispatch, args=[job_id])
        timer.daemon = True
        with self._lock:
            self._timers = [t for t in self._timers if t.is_alive()]
            self._timers.append(timer)
        timer.start()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Block until every dispatched job has finished. For tests."""
        with self._lock:
            pending = list(self._pending)
        _, not_done = wait(pending, timeout=timeout)
        return not not_done

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for timer in self._timers:
                timer.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)
