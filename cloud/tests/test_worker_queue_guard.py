"""An unavailable or quota-exhausted task queue: the platform worker idles with a clear
operator-facing state instead of crashing (and being restarted in a loop), touches no task,
keeps checkpoints, and resumes by itself when the queue answers again."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from cloud.intel.core.context import Ctx
from cloud.intel.platform import Platform
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks import worker as worker_mod
from cloud.shared.queue import InMemoryJobQueue, QueueUnavailable, queue_unavailable

QUOTA = "max requests limit exceeded. Limit: 500000, Usage: 500003. See https://upstash.com/docs/x for details"


class FlakyQueue(InMemoryJobQueue):
    """An in-memory queue whose provider can be switched 'down'."""

    def __init__(self) -> None:
        super().__init__()
        self.down = None          # an exception instance raised by every command while set
        self.reserve_calls = 0
        self.ack_down = None

    def reserve(self, worker_id, *, visibility_timeout):
        self.reserve_calls += 1
        if self.down is not None:
            raise self.down
        return super().reserve(worker_id, visibility_timeout=visibility_timeout)

    def ack(self, delivery):
        if self.ack_down is not None:
            raise self.ack_down
        return super().ack(delivery)

    def heartbeat_worker(self, worker_id):
        if self.down is not None:
            raise self.down
        return super().heartbeat_worker(worker_id)


class ClassificationTests(unittest.TestCase):
    def test_quota_and_connection_errors_are_infrastructure(self) -> None:
        import redis

        quota = queue_unavailable(redis.exceptions.ResponseError(QUOTA))
        self.assertIsInstance(quota, QueueUnavailable)
        self.assertTrue(quota.quota)
        self.assertIn("quota is exhausted", quota.reason)
        self.assertNotIn("upstash.com/docs", quota.reason)            # the reason stays short
        down = queue_unavailable(redis.exceptions.ConnectionError("Connection refused"))
        self.assertEqual((down.quota, "unreachable" in down.reason), (False, True))
        self.assertIsNotNone(queue_unavailable(redis.exceptions.TimeoutError()))
        # a genuine bug (or another Redis error) is NOT swallowed
        self.assertIsNone(queue_unavailable(redis.exceptions.ResponseError("WRONGTYPE Operation against a key")))
        self.assertIsNone(queue_unavailable(ValueError("bad")))


class WorkerGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        import redis

        self.redis = redis
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "W", f"w-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.queue = FlakyQueue()
        self.platform = Platform(self.store, queue=self.queue)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = str(Path(self.tmp.name) / "worker-state.json")

    def state(self):
        return json.loads(Path(self.state_file).read_text(encoding="utf-8"))

    def test_quota_exhausted_queue_idles_without_touching_tasks_then_recovers(self) -> None:
        calls = []
        with mock.patch.object(worker_mod, "resolve_handler", return_value=lambda *a: calls.append(1) or {"ok": 1}):
            task = self.platform.tasks.submit(self.ctx, "analytics")
            paused = self.platform.tasks.submit(self.ctx, "analytics")
            self.store.update(Ctx.for_system(self.ctx.workspace_id), "platform_tasks", paused["id"],
                              {"status": "paused", "progress": {"checkpoint": {"page": 4820}}})
            self.queue.down = self.redis.exceptions.ResponseError(QUOTA)
            w = worker_mod.PlatformWorker(self.platform, worker_id="w1", state_file=self.state_file)
            maintained = []
            w.maintain = lambda: maintained.append(1) or {}

            self.assertFalse(w.process_next())                       # no crash: degraded instead
            self.assertIsNotNone(w.degraded)
            self.assertTrue(w.degraded.quota)
            self.assertGreaterEqual(w._backoff, worker_mod.QUOTA_RETRY_SECONDS[0])
            state = self.state()
            self.assertEqual((state["state"], state["quota"]), ("queue_unavailable", True))
            self.assertIn("quota", state["reason"])
            # nothing was claimed, failed or retried; the paused checkpoint is intact
            self.assertEqual(self.platform.tasks.get(self.ctx, task["id"])["status"], "queued")
            self.assertEqual(self.platform.tasks.get(self.ctx, task["id"])["attempts"], 0)
            kept = self.platform.tasks.get(self.ctx, paused["id"])
            self.assertEqual((kept["status"], kept["progress"]["checkpoint"]), ("paused", {"page": 4820}))

            # the run loop waits for the back-off instead of hammering the queue or maintaining
            with mock.patch.object(worker_mod, "QUOTA_RETRY_SECONDS", (0.2, 0.4)):
                w.degraded = None
                w._last_maintenance = time.monotonic()
                thread = threading.Thread(target=w.run, daemon=True)
                thread.start()
                time.sleep(0.6)
                self.assertTrue(thread.is_alive())                   # still running, not crashed
                self.assertLessEqual(self.queue.reserve_calls, 6)     # a few checks, not a poll loop
                self.assertEqual(maintained, [])                      # maintenance paused while down
                # the queue comes back: the worker resumes by itself and runs the task once
                self.queue.down = None
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and self.platform.tasks.get(self.ctx, task["id"])["status"] != "completed":
                    time.sleep(0.05)
                w.stop()
                thread.join(timeout=5)
        self.assertEqual(self.platform.tasks.get(self.ctx, task["id"])["status"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(w.degraded)
        self.assertEqual(self.state()["state"], "ok")
        self.assertTrue(maintained)                                   # maintenance resumed on recovery

    def test_backoff_grows_and_is_capped(self) -> None:
        w = worker_mod.PlatformWorker(self.platform, worker_id="w1")
        self.queue.down = self.redis.exceptions.ConnectionError("refused")
        waits = []
        for _ in range(8):
            w.process_next()
            waits.append(w._backoff)
        low, high = worker_mod.QUEUE_RETRY_SECONDS
        self.assertEqual(waits[0], low)
        self.assertEqual(waits[1], low * 2)
        self.assertEqual(waits[-1], high)
        self.assertFalse(w.degraded.quota)

    def test_an_unacknowledged_delivery_never_runs_twice(self) -> None:
        calls = []
        with mock.patch.object(worker_mod, "resolve_handler", return_value=lambda *a: calls.append(1) or {}):
            task = self.platform.tasks.submit(self.ctx, "analytics")
            w = worker_mod.PlatformWorker(self.platform, worker_id="w1", lease_seconds=30)
            self.queue.ack_down = self.redis.exceptions.ResponseError(QUOTA)
            self.assertTrue(w.process_next())                        # ran; the ack failure did not crash
            self.assertIsNotNone(w.degraded)
            self.queue.ack_down = None
            self.queue.enqueue(f"{self.ctx.workspace_id}/{task['id']}")   # a redelivery later
            w.degraded = None
            while w.process_next():
                pass
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.platform.tasks.get(self.ctx, task["id"])["status"], "completed")

    def test_other_errors_still_surface(self) -> None:
        w = worker_mod.PlatformWorker(self.platform, worker_id="w1")
        self.queue.down = self.redis.exceptions.ResponseError("WRONGTYPE Operation against a key")
        with self.assertRaises(self.redis.exceptions.ResponseError):
            w.process_next()
        self.assertIsNone(w.degraded)


if __name__ == "__main__":
    unittest.main()
