"""The platform worker: runs every background task kind.

    python -m cloud.intel.tasks.worker --env-file cloud/worker/.env

It runs beside (not instead of) CareerCloud's crawl worker
(``python -m cloud.worker``), on its own queue prefix
(``<CAREERCLOUD_QUEUE_PREFIX>:platform``), so neither can starve or confuse
the other. It is host-neutral: the same command works on a laptop, a VPS or any
cloud VM; only environment variables change.

Loop::

    reserve(visibility) ─► claim (atomic) ─► heartbeat thread ─► handler
        ├─ result             ─► finish (fenced)
        ├─ TaskPaused         ─► paused, attempt refunded, checkpoint kept
        ├─ PermanentTaskError ─► failed, no retry
        ├─ exception          ─► retrying with backoff, or failed when exhausted
        ├─ cancelled          ─► cancelled
        └─ shutting down      ─► queued, attempt refunded
    ack
    maintenance every 30 s: reap expired leases, re-ring orphans, schedule due monitors
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import signal
import socket
import threading
import time
import uuid
from typing import Any, Callable, Dict, Mapping, Optional

from cloud.intel.core.context import Ctx, utcnow
from cloud.intel.tasks.service import TaskReporter, parse_queue_key

__all__ = ["HANDLERS", "PermanentTaskError", "PlatformWorker", "TaskPaused", "run_task_inline"]

log = logging.getLogger(__name__)

#: task kind -> "module:function". ``function(platform, ctx, task, reporter) -> dict``.
HANDLERS: Dict[str, str] = {
    "crawl": "cloud.intel.jobs.crawl_task:run_crawl",
    "discovery": "cloud.intel.discovery.service:run_discovery_task",
    "scraper": "cloud.intel.scraper.runner:run_scrape_task",
    "enrichment": "cloud.intel.providers.contacts:run_enrichment_task",
    "validation": "cloud.intel.email.service:run_validation_task",
    "research": "cloud.intel.research.service:run_research_task",
    "analytics": "cloud.intel.analytics.service:run_analytics_task",
    "workflow": "cloud.intel.automation.engine:run_workflow_task",
    "import_merge": "cloud.intel.imports.service:run_merge_task",
    "monitor": "cloud.intel.monitoring.service:run_monitor_task",
    "signals": "cloud.intel.signals.service:run_signals_task",
    "export": "cloud.intel.exports.service:run_export_task",
    "source_search": "cloud.intel.sources.service:run_source_search_task",
    "email_validation_job": "cloud.intel.email.jobs:run_validation_job_task",
    "email_send": "cloud.intel.sending.outbox:run_send_task",
    "workflow_resume": "cloud.intel.automation.engine:run_workflow_resume_task",
    "scoring": "cloud.intel.scoring.service:run_scoring_task",
    "internal_data": "cloud.intel.imports.internal:run_internal_data_task",
    "integration": "cloud.intel.integrations.service:run_integration_task",
    "job_monitor": "cloud.intel.job_monitor.runner:run_job_monitor_task",
    "job_import": "cloud.intel.job_monitor.importer:run_job_import_task",
    "job_lifecycle": "cloud.intel.job_monitor.runner:run_job_lifecycle_task",
    "job_export": "cloud.intel.job_monitor.exports:run_job_export_task",
}


class TaskPaused(Exception):
    """Raised by a handler at a safe point after ``reporter.should_pause()``."""

    def __init__(self, checkpoint: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__("paused")
        self.checkpoint = dict(checkpoint or {})


class PermanentTaskError(Exception):
    """A failure another attempt cannot fix (bad input, missing credentials)."""


class TaskCancelled(Exception):
    pass


#: Services whose ``tick(ctx) -> int`` runs on every maintenance pass, per workspace.
#: A tick must be cheap when there is nothing due and must never send email unless
#: every sending gate allows it.
PERIODIC_SERVICES = ("outbox", "automation", "job_monitors")


def resolve_handler(kind: str) -> Callable[..., Dict[str, Any]]:
    module_name, _, func = HANDLERS[kind].partition(":")
    return getattr(importlib.import_module(module_name), func)


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class _Heartbeat(threading.Thread):
    def __init__(self, reporter: TaskReporter, interval: float, queue: Any, delivery: Any, visibility: float) -> None:
        super().__init__(daemon=True, name="task-heartbeat")
        self.reporter, self.interval, self.queue, self.delivery, self.visibility = (
            reporter, interval, queue, delivery, visibility)
        self._stop = threading.Event()
        self.lost = False

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                updated = self.reporter.service.heartbeat(self.reporter.ctx, self.reporter.task,
                                                          lease_seconds=self.reporter.lease_seconds)
                if updated is None:
                    self.lost = True
                    return
                if self.queue is not None and self.delivery is not None:
                    self.queue.extend(self.delivery, visibility_timeout=self.visibility)
            except Exception:  # noqa: BLE001
                log.exception("heartbeat failed")

    def stop(self) -> None:
        self._stop.set()


def execute_claimed(platform: Any, ctx: Ctx, claimed: Dict[str, Any], *, lease_seconds: float = 120.0,
                    queue: Any = None, delivery: Any = None, shutting_down: Callable[[], bool] = lambda: False
                    ) -> Optional[Dict[str, Any]]:
    tasks = platform.tasks
    reporter = TaskReporter(tasks, ctx, claimed, lease_seconds=lease_seconds)
    beat = _Heartbeat(reporter, max(1.0, lease_seconds / 3), queue, delivery, lease_seconds * 1.5)
    beat.start()
    try:
        handler = resolve_handler(claimed["kind"])
        result = handler(platform, ctx, claimed, reporter)
        if reporter.is_cancelled():
            return tasks.finish_cancelled(ctx, claimed)
        return tasks.finish(ctx, claimed, result or {})
    except TaskPaused as paused:
        return tasks.finish_paused(ctx, claimed, paused.checkpoint)
    except TaskCancelled:
        return tasks.finish_cancelled(ctx, claimed)
    except PermanentTaskError as error:
        return tasks.fail(ctx, claimed, str(error), retryable=False)
    except Exception as error:  # noqa: BLE001 - any other failure is retryable
        log.exception("task %s (%s) failed", claimed["id"], claimed["kind"])
        if shutting_down():
            return tasks.release(ctx, claimed)
        return tasks.fail(ctx, claimed, f"{type(error).__name__}: {error}", retryable=True)
    finally:
        beat.stop()


def _workspace_ctx(platform: Any, workspace_id: str) -> Ctx:
    info = None
    lookup = getattr(platform.store, "system_membership", None)
    if callable(lookup):
        info = lookup(workspace_id)
    return Ctx.for_system(workspace_id, ai_external_allowed=bool(info and info.get("ai_external_allowed")))


def run_task_inline(platform: Any, workspace_id: str, task_id: str, *, worker_id: str = "inline") -> Optional[Dict]:
    """Claim and run one task in this thread (tests, and the no-Redis dev mode)."""
    ctx = _workspace_ctx(platform, workspace_id)
    claimed = platform.tasks.claim(ctx, task_id, worker_id=worker_id, lease_seconds=120)
    if claimed is None:
        return None
    return execute_claimed(platform, ctx, claimed)


#: Back-off (first wait, cap) while the queue is unreachable, and while its provider
#: refuses commands for a quota: no task is taken, so waiting long costs nothing, and a
#: refused command is not billed — checking every 30 min still notices a reset quickly.
QUEUE_RETRY_SECONDS = (30.0, 900.0)
QUOTA_RETRY_SECONDS = (300.0, 1800.0)
#: When idle the worker blocks on the queue's doorbell; the reserve script (which also
#: promotes delayed retries and expired leases) still runs at least this often.
HOUSEKEEPING_SECONDS = 120.0
#: Lost-doorbell recovery (re-ring queued tasks older than 10 min) runs this often.
ORPHAN_SECONDS = 600.0
#: Worker presence beat; the queue considers a worker gone after 180 s without one.
PRESENCE_SECONDS = 60.0


class PlatformWorker:
    def __init__(self, platform: Any, *, worker_id: Optional[str] = None, lease_seconds: float = 120.0,
                 poll_seconds: float = 2.0, maintenance_seconds: float = 30.0,
                 state_file: Optional[str] = None) -> None:
        self.platform = platform
        self.queue = platform.queue
        if self.queue is None:
            raise ValueError("the platform worker needs a queue")
        self.worker_id = worker_id or default_worker_id()
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.maintenance_seconds = maintenance_seconds
        self._stopping = threading.Event()
        self._last_maintenance = 0.0
        self._last_orphans = 0.0
        self._last_reserve = 0.0
        #: reserve on the next round: a doorbell rang, or the last reserve found work
        self._woken = True
        self._requeue_all = False
        self._last_insights: Dict[str, float] = {}
        #: Proactive AI insights are swept at most this often per workspace.
        self.insight_seconds = 3600.0
        #: Set while the queue is unavailable (see :meth:`_queue_down`); None when healthy.
        self.degraded: Optional[Any] = None
        self._degraded_since: Optional[str] = None
        self._retry_at = 0.0
        self._backoff = 0.0
        self._state_lock = threading.Lock()
        #: Where operators (the Windows supervisor) read the worker's queue state.
        self.state_file = state_file

    def stop(self) -> None:
        self._stopping.set()

    def process_next(self) -> bool:
        from cloud.shared.queue import queue_unavailable

        try:
            self._last_reserve = time.monotonic()
            delivery = self.queue.reserve(self.worker_id, visibility_timeout=self.lease_seconds * 1.5)
        except Exception as error:  # noqa: BLE001 - only an unavailable queue is handled here
            problem = queue_unavailable(error)
            if problem is None:
                raise
            self._queue_down(problem)
            return False
        self._queue_up()
        if delivery is None:
            return False
        try:
            workspace_id, task_id = parse_queue_key(delivery.job_id)
            ctx = _workspace_ctx(self.platform, workspace_id)
            claimed = self.platform.tasks.claim(ctx, task_id, worker_id=self.worker_id,
                                                lease_seconds=self.lease_seconds)
            if claimed is not None:
                execute_claimed(self.platform, ctx, claimed, lease_seconds=self.lease_seconds, queue=self.queue,
                                delivery=delivery, shutting_down=self._stopping.is_set)
        except Exception:  # noqa: BLE001 - a bad delivery must not kill the loop
            log.exception("could not process delivery %s", delivery.job_id)
        finally:
            try:
                self.queue.ack(delivery)
            except Exception as error:  # noqa: BLE001
                problem = queue_unavailable(error)
                if problem is None:
                    raise
                # The task's own state is already recorded in the database. The un-acked
                # delivery comes back after its visibility timeout, and the claim then
                # refuses a task that is no longer queued, so nothing runs twice.
                self._queue_down(problem)
        return True

    # ------------------------------------------------------------------ queue availability

    def _queue_down(self, problem: Any) -> None:
        """Enter (or stay in) the 'queue unavailable' state: no task is reserved, claimed or
        failed; maintenance pauses; the queue is re-checked after a growing back-off."""
        with self._state_lock:
            first = self.degraded is None
            low, high = QUOTA_RETRY_SECONDS if problem.quota else QUEUE_RETRY_SECONDS
            self._backoff = low if first else min(high, max(low, self._backoff * 2))
            self._retry_at = time.monotonic() + self._backoff
            self.degraded = problem
            if first:
                self._degraded_since = utcnow().isoformat()
        if first:
            log.error("TASK QUEUE UNAVAILABLE: %s. The worker stays running but idle: no task is taken or "
                      "failed, queued tasks keep their state and checkpoints in PostgreSQL, and maintenance "
                      "is paused. Next queue check in %d s.", problem.reason, self._backoff)
        else:
            log.warning("task queue still unavailable (%s); next check in %d s", problem.reason, self._backoff)
        self._write_state()

    def _queue_up(self) -> None:
        if self.degraded is None:
            return
        with self._state_lock:
            if self.degraded is None:
                return
            since, self.degraded, self._degraded_since = self._degraded_since, None, None
            self._backoff, self._retry_at = 0.0, 0.0
            self._last_maintenance = 0.0          # run maintenance now: it re-rings tasks queued meanwhile
            self._last_orphans = 0.0              # ... including those submitted while the queue was down
            self._requeue_all = True              # whatever their age (enqueue and claim are idempotent)
            self._woken = True
        log.warning("task queue available again (unavailable since %s); resuming work", since)
        self._write_state()

    def _waiting_for_queue(self) -> bool:
        """While the queue is unavailable, sleep until the next check (or a stop). True when
        the caller should skip this round."""
        if self.degraded is None:
            return False
        remaining = self._retry_at - time.monotonic()
        if remaining > 0:
            self._stopping.wait(remaining)
            return True
        return False

    def _write_state(self) -> None:
        if not self.state_file:
            return
        problem = self.degraded
        state = {"state": "queue_unavailable" if problem is not None else "ok",
                 "reason": getattr(problem, "reason", None), "quota": bool(getattr(problem, "quota", False)),
                 "since": self._degraded_since,
                 "next_check_in_s": round(max(0.0, self._retry_at - time.monotonic())) if problem else None,
                 "worker_id": self.worker_id, "updated_at": utcnow().isoformat()}
        try:
            path = os.path.abspath(self.state_file)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            os.replace(tmp, path)
        except OSError:
            log.warning("could not write the worker state file %s", self.state_file, exc_info=True)

    def maintain(self) -> Dict[str, int]:
        report = {"reaped": 0, "requeued": 0, "monitors": 0}
        orphans = time.monotonic() - self._last_orphans >= ORPHAN_SECONDS
        if orphans:
            self._last_orphans = time.monotonic()
        everything, self._requeue_all = self._requeue_all, False
        for workspace_id in self.platform.store.list_workspace_ids():
            ctx = Ctx.for_system(workspace_id)
            try:
                report["reaped"] += len(self.platform.tasks.reap(ctx))
                # re-ringing every queued task older than 10 min is a queue command per task:
                # every ORPHAN_SECONDS is enough (those tasks are at least that old anyway)
                if orphans:
                    report["requeued"] += self.platform.tasks.requeue_orphans(
                        ctx, **({"older_than_seconds": 0} if everything else {}))
                report["monitors"] += self.platform.service("monitoring").schedule_due(ctx)
                # Scraper runs whose task died with the worker: mark them so they can be retried.
                report["scrape_recovered"] = report.get("scrape_recovered", 0) +                     self.platform.service("scraper").recover(ctx)
                # Sequence steps advance only where sending is actually permitted
                # (production + explicit flag). Elsewhere every send would just be
                # recorded as "blocked" again each cycle, so nothing is scheduled.
                if time.monotonic() - self._last_insights.get(workspace_id, -1e9) >= self.insight_seconds:
                    self._last_insights[workspace_id] = time.monotonic()
                    report["insights"] = report.get("insights", 0) + len(
                        self.platform.service("insights").generate(ctx))
                # Due sequence steps advance in the "outbox" periodic tick below, which is a
                # no-op unless sending is permitted (production + explicit flag).
            except Exception:  # noqa: BLE001
                log.exception("maintenance failed for workspace %s", workspace_id)
            # Periodic service ticks (outbox, delayed workflows, scheduled work). Each is
            # isolated: one failing service never stops the others or the next workspace.
            for name in PERIODIC_SERVICES:
                try:
                    tick = getattr(self.platform.service(name), "tick", None)
                    if callable(tick):
                        done = tick(ctx) or 0
                        report[f"tick_{name}"] = report.get(f"tick_{name}", 0) + int(done)
                except Exception:  # noqa: BLE001
                    log.exception("periodic %s failed for workspace %s", name, workspace_id)
        return report

    def run(self, concurrency: int = 1) -> None:
        """Process tasks until stopped. ``concurrency`` > 1 runs that many tasks at once
        (e.g. several scraper runs), each on its own thread; maintenance stays on this one."""
        log.info("platform worker %s started (concurrency %s)", self.worker_id, concurrency)
        presence = threading.Thread(target=self._presence_loop, name="platform-worker-presence", daemon=True)
        presence.start()
        extra = [threading.Thread(target=self._loop, name=f"platform-worker-{n}", daemon=True)
                 for n in range(1, max(1, concurrency))]
        for thread in extra:
            thread.start()
        self._write_state()
        while not self._stopping.is_set():
            if self._waiting_for_queue():
                continue
            now = time.monotonic()
            if self.degraded is None and now - self._last_maintenance >= self.maintenance_seconds:
                self._last_maintenance = now
                self.maintain()
            self._round()
        for thread in extra:
            thread.join(timeout=self.lease_seconds)
        presence.join(timeout=5)
        try:
            self.queue.forget_worker(self.worker_id)
        except Exception:  # noqa: BLE001 - the entry expires on its own
            log.debug("could not remove the worker presence entry", exc_info=True)
        log.info("platform worker %s stopped", self.worker_id)

    def presence_interval(self) -> float:
        """How often the worker announces itself: well inside the staleness window, so a
        monitor never mistakes a long-running task for a dead worker."""
        from cloud.shared.queue import DEFAULT_WORKER_STALE_AFTER

        return max(1.0, min(PRESENCE_SECONDS, DEFAULT_WORKER_STALE_AFTER / 3))

    def _presence_loop(self) -> None:
        """Worker presence on its own thread. Before this, the beat ran between tasks on
        the main loop, so any task longer than the staleness window (90 s) made a healthy
        worker look dead and the supervisor restarted it mid-task."""
        interval = self.presence_interval()
        while True:
            # no beat while the queue is unavailable: it would only fail (and log) every time
            if self.degraded is None:
                try:
                    self.queue.heartbeat_worker(self.worker_id)
                except Exception:  # noqa: BLE001 - a missed beat must never stop the worker
                    log.warning("worker presence heartbeat failed", exc_info=True)
            self._write_state()                  # local liveness for the supervisor (no queue command)
            if self._stopping.wait(interval):
                return

    def _loop(self) -> None:
        while not self._stopping.is_set():
            if self._waiting_for_queue():
                continue
            self._round()

    def _round(self) -> None:
        """Reserve only when there may be work (a doorbell, work just found, or the
        housekeeping interval is due); otherwise wait on the doorbell."""
        # a worker waiting for the queue probes it with a reserve when its back-off ends
        if self.degraded is not None or self._woken or time.monotonic() - self._last_reserve >= HOUSEKEEPING_SECONDS:
            self._woken = self.process_next()
            if self._woken or self.degraded is not None:
                return
        self._woken = self._idle_wait()

    def _idle_wait(self) -> bool:
        """Nothing to reserve: wait for the queue's doorbell (one blocking command per wait)
        until the next maintenance pass or housekeeping reserve is due. True when the
        doorbell rang (reserve now)."""
        until = min(self._last_reserve + HOUSEKEEPING_SECONDS, self._last_maintenance + self.maintenance_seconds)
        # at least 1 s (the shortest blocking wait), so the doorbell is always checked
        remaining = max(1.0, until - time.monotonic())
        from cloud.shared.queue import queue_unavailable

        try:
            return bool(self.queue.wait_for_work(remaining))
        except Exception as error:  # noqa: BLE001
            problem = queue_unavailable(error)
            if problem is None:
                raise
            self._queue_down(problem)
            return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CareerCrawler platform worker")
    parser.add_argument("--env-file")
    parser.add_argument("--once", action="store_true", help="run maintenance and at most one task, then exit")
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("CAREERCLOUD_PLATFORM_CONCURRENCY", "1")),
                        help="tasks processed at once (default 1)")
    parser.add_argument("--state-file", default=os.environ.get("CAREERCLOUD_WORKER_STATE_FILE"),
                        help="JSON file where the worker reports its queue state for operators")
    args = parser.parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    logging.basicConfig(level=os.environ.get("CAREERCLOUD_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from cloud.intel.bootstrap import build_platform

    platform = build_platform(role="worker")
    worker = PlatformWorker(platform, state_file=args.state_file)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop())
    try:
        if args.once:
            print(worker.maintain())
            worker.process_next()
        else:
            worker.run(concurrency=max(1, min(args.concurrency, 16)))
    finally:
        platform.close()
    return 0


if __name__ == "__main__":
    # ``python -m cloud.intel.tasks.worker`` loads this file as ``__main__``; handlers import
    # ``cloud.intel.tasks.worker`` and would get a SECOND copy of TaskPaused / TaskCancelled /
    # PermanentTaskError that this loop's ``except`` clauses never match (a pause or cancel then
    # looked like a crash and was retried). Run the canonical module's loop instead.
    from cloud.intel.tasks import worker as _canonical

    raise SystemExit(_canonical.main())
