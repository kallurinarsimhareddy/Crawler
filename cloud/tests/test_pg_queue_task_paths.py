"""Every background task type through the real worker path on the PostgreSQL task queue.

The feature suites below run their tasks with ``run_task_inline`` (claim + execute in the
test thread). Here the same tests run again with that function replaced by the production
path: the PostgreSQL queue's ``reserve`` (due-task query, priority, cooldown) feeding
``PlatformWorker.process_next`` (claim, heartbeat, finish / retry / pause / cancel, ack).
Their own assertions then check each task kind — job monitor, lifecycle, CSV import /
export, Account Builder, contacts / enrichment, email validation, signals, scraper,
discovery, research, workflows, analytics, automation and the rest.
"""

from __future__ import annotations

import importlib
import unittest
from typing import Any, Dict, Optional
from unittest import mock

from cloud.intel.core.context import Ctx
from cloud.intel.tasks import worker as worker_mod
from cloud.intel.tasks.pgqueue import PostgresTaskQueue

MODULES = (
    "test_account_builder", "test_job_csv", "test_job_lifecycle", "test_job_monitor", "test_job_monitor_sweep",
    "test_job_relevance", "test_platform_acceptance", "test_platform_agent", "test_platform_analytics",
    "test_platform_automation", "test_platform_contacts", "test_platform_crawl", "test_platform_crm_api",
    "test_platform_discovery", "test_platform_email", "test_platform_email_engine",
    "test_platform_email_hardening", "test_platform_email_jobs", "test_platform_exports",
    "test_platform_imports", "test_platform_internal_data", "test_platform_research", "test_platform_scraper",
    "test_platform_scraper_v2", "test_platform_signals", "test_platform_sources", "test_platform_triggers",
    "test_platform_workflows",
)

#: How many tasks went through the worker path, by kind (reported by the final check).
SEEN: Dict[str, int] = {}


class _Platform:
    """The test's platform with the PostgreSQL queue in place of whatever it was built with."""

    def __init__(self, platform: Any, queue: Any) -> None:
        self._platform = platform
        self.queue = queue

    def __getattr__(self, name: str) -> Any:
        return getattr(self._platform, name)


def via_postgres_queue(platform: Any, workspace_id: str, task_id: str, *, worker_id: str = "pgq") -> Optional[Dict]:
    """``run_task_inline``'s contract (None when the task is not claimable, else the task after
    the run) through the PostgreSQL queue and PlatformWorker.process_next."""
    sys_ctx = Ctx.for_system(workspace_id)
    before = platform.tasks.get(sys_ctx, task_id)
    queue = PostgresTaskQueue(platform.store, clock=getattr(platform.tasks, "_clock", None), only_ids=[task_id])
    worker = worker_mod.PlatformWorker(_Platform(platform, queue), worker_id=worker_id, lease_seconds=120)
    if not worker.process_next():
        return None
    after = platform.tasks.get(sys_ctx, task_id)
    if after["attempts"] == before["attempts"] and after["status"] == before["status"]:
        return None                       # reserved but not claimable (the inline call returned None too)
    SEEN[after["kind"]] = SEEN.get(after["kind"], 0) + 1
    return after


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite()
    patches = [mock.patch.object(worker_mod, "run_task_inline", via_postgres_queue)]
    for name in MODULES:
        module = importlib.import_module(f"cloud.tests.{name}")
        if hasattr(module, "run_task_inline"):
            patches.append(mock.patch.object(module, "run_task_inline", via_postgres_queue))
        suite.addTests(loader.loadTestsFromModule(module))

    class Patched(unittest.TestSuite):
        def run(self, result, debug=False):
            for p in patches:
                p.start()
            try:
                return super().run(result, debug)
            finally:
                for p in patches:
                    p.stop()
                print("\ntask kinds run through the PostgreSQL queue + worker:",
                      ", ".join(f"{k} ({v})" for k, v in sorted(SEEN.items())))

    return Patched([suite])
