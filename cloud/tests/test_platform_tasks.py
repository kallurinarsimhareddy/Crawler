"""Platform task lifecycle: claims, leases, retries, pause/resume, cancel,
dead-worker recovery, idempotency, and the worker loop over a real queue."""

from __future__ import annotations

import unittest
import uuid
from datetime import timedelta
from unittest import mock

from cloud.intel.core.context import ConflictError, Ctx, utcnow
from cloud.intel.platform import Platform
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks import worker as worker_mod
from cloud.intel.tasks.service import TaskService
from cloud.shared.queue import InMemoryJobQueue


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class TaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "W", "w-tasks")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.sys = Ctx.for_system(ws["id"])
        self.clock = Clock()
        self.queue = InMemoryJobQueue()
        self.tasks = TaskService(self.store, queue=self.queue, clock=self.clock)

    def test_submit_is_idempotent_and_enqueues(self) -> None:
        a = self.tasks.submit(self.ctx, "analytics", {"x": 1}, idempotency_key="k1")
        b = self.tasks.submit(self.ctx, "analytics", {"x": 1}, idempotency_key="k1")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.queue.stats().ready, 1)
        with self.assertRaises(ConflictError):
            self.tasks.submit(self.ctx, "export", {}, idempotency_key="k1")

    def test_claim_is_exclusive(self) -> None:
        task = self.tasks.submit(self.ctx, "analytics")
        first = self.tasks.claim(self.sys, task["id"], worker_id="w1", lease_seconds=60)
        second = self.tasks.claim(self.sys, task["id"], worker_id="w2", lease_seconds=60)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(first["attempts"], 1)

    def test_retry_backoff_then_fail_when_exhausted(self) -> None:
        task = self.tasks.submit(self.ctx, "analytics", max_attempts=2)
        claimed = self.tasks.claim(self.sys, task["id"], worker_id="w1", lease_seconds=60)
        retried = self.tasks.fail(self.sys, claimed, "boom")
        self.assertEqual(retried["status"], "retrying")
        self.assertIsNone(self.tasks.claim(self.sys, task["id"], worker_id="w1", lease_seconds=60))  # not due
        self.clock.advance(31)
        claimed = self.tasks.claim(self.sys, task["id"], worker_id="w1", lease_seconds=60)
        self.assertEqual(claimed["attempts"], 2)
        failed = self.tasks.fail(self.sys, claimed, "boom again")
        self.assertEqual(failed["status"], "failed")

    def test_stale_worker_is_fenced_out(self) -> None:
        task = self.tasks.submit(self.ctx, "analytics", max_attempts=3)
        old = self.tasks.claim(self.sys, task["id"], worker_id="w1", lease_seconds=60)
        self.clock.advance(61)
        recovered = self.tasks.reap(self.sys)
        self.assertEqual([t["status"] for t in recovered], ["retrying"])
        self.clock.advance(60)
        new = self.tasks.claim(self.sys, task["id"], worker_id="w2", lease_seconds=60)
        self.assertIsNotNone(new)
        # the old worker wakes up and tries to finish: nothing is written
        self.assertIsNone(self.tasks.finish(self.sys, old, {"stale": True}))
        self.assertIsNone(self.tasks.heartbeat(self.sys, old, lease_seconds=60))
        done = self.tasks.finish(self.sys, new, {"ok": True})
        self.assertEqual(done["result"], {"ok": True})

    def test_reap_fails_when_attempts_exhausted_and_cancels_when_requested(self) -> None:
        t1 = self.tasks.submit(self.ctx, "analytics", max_attempts=1)
        self.tasks.claim(self.sys, t1["id"], worker_id="w", lease_seconds=10)
        t2 = self.tasks.submit(self.ctx, "analytics", max_attempts=3)
        self.tasks.claim(self.sys, t2["id"], worker_id="w", lease_seconds=10)
        self.tasks.cancel(self.ctx, t2["id"])
        self.clock.advance(11)
        statuses = {t["id"]: t["status"] for t in self.tasks.reap(self.sys)}
        self.assertEqual(statuses, {t1["id"]: "failed", t2["id"]: "cancelled"})

    def test_pause_resume_cancel(self) -> None:
        task = self.tasks.submit(self.ctx, "analytics")
        self.assertEqual(self.tasks.pause(self.ctx, task["id"])["status"], "paused")
        self.assertIsNone(self.tasks.claim(self.sys, task["id"], worker_id="w", lease_seconds=60))
        self.assertEqual(self.tasks.resume(self.ctx, task["id"])["status"], "queued")
        self.assertEqual(self.tasks.cancel(self.ctx, task["id"])["status"], "cancelled")
        with self.assertRaises(ConflictError):
            self.tasks.cancel(self.ctx, task["id"])

    def test_viewer_cannot_submit(self) -> None:
        viewer = str(uuid.uuid4())
        self.store.add_member(self.ctx, viewer, "viewer")
        from cloud.intel.core.context import ForbiddenError

        with self.assertRaises(ForbiddenError):
            self.tasks.submit(Ctx(self.ctx.workspace_id, viewer, "viewer"), "analytics")


class WorkerLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "W", "w-loop")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.platform = Platform(self.store, queue=InMemoryJobQueue())

    def _run_with(self, handler):
        with mock.patch.object(worker_mod, "resolve_handler", return_value=handler):
            task = self.platform.tasks.submit(self.ctx, "analytics")
            w = worker_mod.PlatformWorker(self.platform, worker_id="w1", lease_seconds=30)
            self.assertTrue(w.process_next())
            return self.platform.tasks.get(self.ctx, task["id"])

    def test_success(self) -> None:
        task = self._run_with(lambda p, c, t, r: {"rows": 3})
        self.assertEqual((task["status"], task["result"]), ("completed", {"rows": 3}))

    def test_permanent_failure_is_not_retried(self) -> None:
        def handler(p, c, t, r):
            raise worker_mod.PermanentTaskError("missing credentials")

        task = self._run_with(handler)
        self.assertEqual(task["status"], "failed")
        self.assertIn("missing credentials", task["error"])

    def test_crash_is_retried(self) -> None:
        def handler(p, c, t, r):
            raise RuntimeError("network down")

        task = self._run_with(handler)
        self.assertEqual(task["status"], "retrying")

    def test_pause_keeps_a_checkpoint_and_refunds_the_attempt(self) -> None:
        def handler(p, c, t, r):
            raise worker_mod.TaskPaused({"done": 7})

        task = self._run_with(handler)
        self.assertEqual(task["status"], "paused")
        self.assertEqual(task["attempts"], 0)
        self.assertEqual(task["progress"]["checkpoint"], {"done": 7})

    def test_duplicate_delivery_runs_once(self) -> None:
        calls = []
        with mock.patch.object(worker_mod, "resolve_handler", return_value=lambda *a: calls.append(1) or {}):
            task = self.platform.tasks.submit(self.ctx, "analytics")
            self.platform.queue.enqueue(f"{self.ctx.workspace_id}/{task['id']}")  # already waiting: ignored
            w = worker_mod.PlatformWorker(self.platform, worker_id="w1")
            while w.process_next():
                pass
            # a late redelivery after completion is ignored too
            self.platform.queue.enqueue(f"{self.ctx.workspace_id}/{task['id']}")
            w.process_next()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
