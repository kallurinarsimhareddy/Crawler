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
import logging
import os
import signal
import socket
import threading
import time
import uuid
from typing import Any, Callable, Dict, Mapping, Optional

from cloud.intel.core.context import Ctx
from cloud.intel.tasks.service import TaskReporter, parse_queue_key

__all__ = ["HANDLERS", "PermanentTaskError", "PlatformWorker", "TaskPaused", "run_task_inline"]

log = logging.getLogger(__name__)

#: task kind -> "module:function". ``function(platform, ctx, task, reporter) -> dict``.
HANDLERS: Dict[str, str] = {
    "crawl": "cloud.intel.jobs.crawl_task:run_crawl",
    "discovery": "cloud.intel.discovery.service:run_discovery_task",
    "scraper": "cloud.intel.scraper.service:run_scrape_task",
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


class PlatformWorker:
    def __init__(self, platform: Any, *, worker_id: Optional[str] = None, lease_seconds: float = 120.0,
                 poll_seconds: float = 2.0, maintenance_seconds: float = 30.0) -> None:
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

    def stop(self) -> None:
        self._stopping.set()

    def process_next(self) -> bool:
        delivery = self.queue.reserve(self.worker_id, visibility_timeout=self.lease_seconds * 1.5)
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
            self.queue.ack(delivery)
        return True

    def maintain(self) -> Dict[str, int]:
        report = {"reaped": 0, "requeued": 0, "monitors": 0}
        for workspace_id in self.platform.store.list_workspace_ids():
            ctx = Ctx.for_system(workspace_id)
            try:
                report["reaped"] += len(self.platform.tasks.reap(ctx))
                report["requeued"] += self.platform.tasks.requeue_orphans(ctx)
                report["monitors"] += self.platform.service("monitoring").schedule_due(ctx)
            except Exception:  # noqa: BLE001
                log.exception("maintenance failed for workspace %s", workspace_id)
        return report

    def run(self) -> None:
        log.info("platform worker %s started", self.worker_id)
        while not self._stopping.is_set():
            now = time.monotonic()
            if now - self._last_maintenance >= self.maintenance_seconds:
                self._last_maintenance = now
                self.queue.heartbeat_worker(self.worker_id)
                self.maintain()
            if not self.process_next():
                self._stopping.wait(self.poll_seconds)
        self.queue.forget_worker(self.worker_id)
        log.info("platform worker %s stopped", self.worker_id)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CareerCrawler platform worker")
    parser.add_argument("--env-file")
    parser.add_argument("--once", action="store_true", help="run maintenance and at most one task, then exit")
    args = parser.parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    logging.basicConfig(level=os.environ.get("CAREERCLOUD_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from cloud.intel.bootstrap import build_platform

    platform = build_platform(role="worker")
    worker = PlatformWorker(platform)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop())
    try:
        if args.once:
            print(worker.maintain())
            worker.process_next()
        else:
            worker.run()
    finally:
        platform.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
