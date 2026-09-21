"""The worker process: take deliveries from the queue, run them, recover the lost.

One :class:`Worker` runs ``concurrency`` job threads and one maintenance loop:

**Job threads** reserve a delivery, hand the id to :class:`JobExecutor`, and
acknowledge the delivery whatever happened — the job's own state lives in
PostgreSQL, so an acknowledged delivery never loses work. While a job runs,
each heartbeat also extends the delivery's visibility timeout.

**Maintenance** runs every ``reap_interval`` seconds, on every worker (all of
its writes are conditional, so several workers reaping at once is safe):

* *stale leases* — running jobs whose worker stopped heartbeating are requeued
  with a backoff delay, failed if they have used their attempts, or cancelled if
  cancellation was requested (:meth:`JobService.reap_stale`);
* *orphans* — queued jobs untouched for ``orphan_after_seconds`` are enqueued
  again, in case the API crashed between committing the job and enqueueing it
  or the queue lost the id. The enqueue is idempotent.

**Presence**: every maintenance tick the worker also stamps its own liveness on
the queue, so the API can report whether any worker is running even when nothing
is queued. A clean shutdown removes the stamp; a killed worker's simply goes
stale. See :meth:`cloud.shared.queue.JobQueue.worker_presence`.

**Shutdown** (SIGINT/SIGTERM): stop taking deliveries; running jobs see
``is_cancelled()`` become true, stop between companies, and are released back
to the queue with their attempt refunded, so a deploy never burns a retry.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional

from cloud.shared.models import JobType
from cloud.shared.queue import JobQueue
from cloud.shared.service import JobService, ReapAction, RetryPolicy
from cloud.worker.executor import JobExecutor
from cloud.worker.workspace import sweep_abandoned_workspaces

__all__ = ["MaintenanceReport", "Worker", "WorkerConfig"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerConfig:
    concurrency: int = 1
    poll_interval: float = 2.0
    visibility_timeout: float = 300.0
    reap_interval: float = 30.0
    orphan_after_seconds: float = 600.0
    reap_batch: int = 100
    #: Workspaces untouched this long are swept (a killed worker's leftovers).
    workspace_max_idle_seconds: float = 6 * 3600.0

    def __post_init__(self) -> None:
        if not 1 <= self.concurrency <= 32:
            raise ValueError("concurrency must be between 1 and 32")
        if self.poll_interval <= 0 or self.visibility_timeout <= 0 or self.reap_interval <= 0:
            raise ValueError("intervals must be positive")


@dataclass
class MaintenanceReport:
    requeued: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    cancelled: List[str] = field(default_factory=list)
    orphans_enqueued: List[str] = field(default_factory=list)
    workspaces_removed: List[str] = field(default_factory=list)


class Worker:
    def __init__(
        self,
        service: JobService,
        queue: JobQueue,
        executor: JobExecutor,
        *,
        config: WorkerConfig = WorkerConfig(),
        retry_policy: Optional[RetryPolicy] = None,
        stopping: Optional[threading.Event] = None,
    ) -> None:
        self._service = service
        self._queue = queue
        self._executor = executor
        self._config = config
        self._policy = retry_policy or RetryPolicy()
        self.stopping = stopping or threading.Event()

    @property
    def worker_id(self) -> str:
        return self._executor.worker_id

    @property
    def runnable_types(self) -> FrozenSet[JobType]:
        return self._executor.runner.supported_types

    def process_next(self) -> bool:
        """Handle at most one delivery. Returns whether there was one."""
        delivery = self._queue.reserve(self.worker_id, visibility_timeout=self._config.visibility_timeout)
        if delivery is None:
            return False
        try:
            if delivery.delivery_count > 1:
                log.info("redelivery %s of %s", delivery.delivery_count, delivery.job_id)
            self._executor.execute(
                delivery.job_id,
                on_heartbeat=lambda: self._queue.extend(
                    delivery, visibility_timeout=self._config.visibility_timeout
                ),
            )
        except Exception:
            # Unknown job id, database outage… The delivery is still acknowledged:
            # a job that exists is recovered by the maintenance loop, one that does
            # not has nothing to recover.
            log.exception("delivery of %s could not be processed", delivery.job_id)
        finally:
            self._queue.ack(delivery)
        return True

    def heartbeat(self) -> None:
        """Tell the queue this worker is alive. Never fatal: presence is a hint."""
        try:
            self._queue.heartbeat_worker(self.worker_id)
        except Exception:
            log.warning("could not record worker presence", exc_info=True)

    def maintain(self) -> MaintenanceReport:
        """One pass of stale-lease reaping and orphan re-enqueueing."""
        report = MaintenanceReport()
        by_action: Dict[str, List[str]] = {
            ReapAction.REQUEUED: report.requeued,
            ReapAction.FAILED: report.failed,
            ReapAction.CANCELLED: report.cancelled,
        }
        for outcome in self._service.reap_stale(self._policy, limit=self._config.reap_batch):
            by_action[outcome.action].append(outcome.job.job_id)
            if outcome.action == ReapAction.REQUEUED:
                self._queue.enqueue(outcome.job.job_id, delay_seconds=outcome.delay_seconds)

        for orphan in self._service.find_orphaned(
            older_than_seconds=self._config.orphan_after_seconds, limit=self._config.reap_batch
        ):
            if orphan.type not in self.runnable_types or orphan.cancel_requested:
                continue
            if self._queue.enqueue(orphan.job_id):
                report.orphans_enqueued.append(orphan.job_id)
            # Either way the job is known to the queue now; don't sweep it again soon.
            self._service.touch(orphan)
        runtime_root = self._executor.runtime_root
        if runtime_root is not None:
            try:
                report.workspaces_removed = sweep_abandoned_workspaces(
                    runtime_root, older_than_seconds=self._config.workspace_max_idle_seconds
                )
            except Exception:
                log.warning("workspace sweep failed", exc_info=True)
        if any((report.requeued, report.failed, report.cancelled, report.orphans_enqueued, report.workspaces_removed)):
            log.info(
                "maintenance: requeued=%d failed=%d cancelled=%d orphans=%d workspaces_removed=%d",
                len(report.requeued),
                len(report.failed),
                len(report.cancelled),
                len(report.orphans_enqueued),
                len(report.workspaces_removed),
            )
        return report

    def _job_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                worked = self.process_next()
            except Exception:
                log.exception("queue unavailable; backing off")
                worked = False
            if not worked:
                self.stopping.wait(self._config.poll_interval)

    def run(self) -> None:
        """Run until :attr:`stopping` is set."""
        log.info(
            "worker %s started: runner=%s concurrency=%d queue=%s",
            self.worker_id,
            self._executor.runner.name,
            self._config.concurrency,
            self._queue.name,
        )
        threads = [
            threading.Thread(target=self._job_loop, name=f"job-loop-{index}", daemon=True)
            for index in range(self._config.concurrency)
        ]
        for thread in threads:
            thread.start()
        # Announce before the first maintenance pass so the dashboard flips to
        # "online" as soon as the process is up, not one reap interval later.
        self.heartbeat()
        try:
            while not self.stopping.is_set():
                try:
                    self.maintain()
                except Exception:
                    log.exception("maintenance pass failed")
                self.stopping.wait(self._config.reap_interval)
                if not self.stopping.is_set():
                    self.heartbeat()
            for thread in threads:
                thread.join(timeout=120)
        finally:
            # A clean stop retracts presence at once; a kill leaves it to go stale.
            try:
                self._queue.forget_worker(self.worker_id)
            except Exception:
                log.warning("could not clear worker presence", exc_info=True)
        log.info("worker %s stopped", self.worker_id)
