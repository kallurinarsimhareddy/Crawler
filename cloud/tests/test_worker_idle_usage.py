"""Idle queue usage: the worker waits on a doorbell instead of polling, still promotes
delayed work, recovers tasks submitted while the queue was down (exactly once), and pause /
cancel keep working with the new loop."""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from unittest import mock

from cloud.intel.core.context import Ctx
from cloud.intel.platform import Platform
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks import worker as worker_mod
from cloud.shared.queue import InMemoryJobQueue, RedisJobQueue
from cloud.tests._optional import needs_module, needs_repo_files

QUOTA = "max requests limit exceeded. Limit: 500000, Usage: 500003."


class CountingQueue(InMemoryJobQueue):
    def __init__(self) -> None:
        super().__init__()
        self.reserves = 0
        self.down = None

    def reserve(self, worker_id, *, visibility_timeout):
        self.reserves += 1
        if self.down is not None:
            raise self.down
        return super().reserve(worker_id, visibility_timeout=visibility_timeout)

    def enqueue(self, job_id, *, delay_seconds=0.0):
        if self.down is not None:
            raise self.down
        return super().enqueue(job_id, delay_seconds=delay_seconds)


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        user = str(uuid.uuid4())
        ws = self.store.create_workspace(user, "W", f"w-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], user, "owner")
        self.queue = CountingQueue()
        self.platform = Platform(self.store, queue=self.queue)

    def start(self, worker):
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        self.addCleanup(lambda: (worker.stop(), thread.join(timeout=5)))
        return thread

    def wait_status(self, task_id, status, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.platform.tasks.get(self.ctx, task_id)["status"] == status:
                return True
            time.sleep(0.02)
        return False


class IdleTests(Harness):
    def test_idle_worker_reserves_rarely_and_wakes_instantly_on_new_work(self) -> None:
        calls = []
        with mock.patch.object(worker_mod, "HOUSEKEEPING_SECONDS", 1.0), \
                mock.patch.object(worker_mod, "resolve_handler", return_value=lambda *a: calls.append(1) or {}):
            w = worker_mod.PlatformWorker(self.platform, worker_id="w1", maintenance_seconds=60)
            w.maintain = lambda: {}
            self.start(w)
            time.sleep(2.2)
            idle_reserves = self.queue.reserves
            # the old loop polled every 2 s *and* after every pass; now: ~1 per housekeeping second
            self.assertLessEqual(idle_reserves, 4)
            started = time.monotonic()
            task = self.platform.tasks.submit(self.ctx, "analytics")
            self.assertTrue(self.wait_status(task["id"], "completed", 2.0))
            self.assertLess(time.monotonic() - started, 0.9)          # the doorbell, not a timer
        self.assertEqual(len(calls), 1)

    def test_delayed_work_is_still_promoted_by_housekeeping(self) -> None:
        with mock.patch.object(worker_mod, "HOUSEKEEPING_SECONDS", 0.5), \
                mock.patch.object(worker_mod, "resolve_handler", return_value=lambda *a: {}):
            task = self.platform.tasks.submit(self.ctx, "analytics")
            # simulate a retry backoff: the doorbell does not ring for delayed work
            w = worker_mod.PlatformWorker(self.platform, worker_id="w1", maintenance_seconds=60)
            w.maintain = lambda: {}
            self.assertTrue(w.process_next())
            again = self.platform.tasks.submit(self.ctx, "analytics")
            self.queue._ready.clear(); self.queue._waiting.clear(); self.queue._rang = False
            self.queue.enqueue(f"{self.ctx.workspace_id}/{again['id']}", delay_seconds=0.3)
            self.start(w)
            self.assertTrue(self.wait_status(again["id"], "completed", 3.0))
        self.assertEqual(self.platform.tasks.get(self.ctx, task["id"])["status"], "completed")

    def test_orphans_are_re_rung_every_ten_minutes_not_every_pass(self) -> None:
        w = worker_mod.PlatformWorker(self.platform, worker_id="w1")
        with mock.patch.object(self.platform.tasks, "requeue_orphans", return_value=0) as requeue:
            w.maintain(); w.maintain(); w.maintain()
            self.assertEqual(requeue.call_count, 1)
            w._last_orphans -= worker_mod.ORPHAN_SECONDS
            w.maintain()
            self.assertEqual(requeue.call_count, 2)

    def test_presence_every_minute_inside_the_stale_window(self) -> None:
        from cloud.shared.queue import DEFAULT_WORKER_STALE_AFTER

        w = worker_mod.PlatformWorker(self.platform, worker_id="w1")
        self.assertEqual(w.presence_interval(), 60.0)
        self.assertGreaterEqual(DEFAULT_WORKER_STALE_AFTER, 3 * w.presence_interval())


class OutageRecoveryTests(Harness):
    @needs_module("redis", "fakeredis")
    def test_tasks_submitted_while_the_queue_is_down_run_once_after_recovery(self) -> None:
        import redis

        calls = []
        with mock.patch.object(worker_mod, "QUOTA_RETRY_SECONDS", (0.2, 0.4)), \
                mock.patch.object(worker_mod, "resolve_handler", return_value=lambda *a: calls.append(1) or {}):
            self.queue.down = redis.exceptions.ResponseError(QUOTA)
            task = self.platform.tasks.submit(self.ctx, "analytics")       # enqueue fails: Postgres keeps it
            self.assertEqual(self.platform.tasks.get(self.ctx, task["id"])["status"], "queued")
            w = worker_mod.PlatformWorker(self.platform, worker_id="w1", maintenance_seconds=0.2)
            self.start(w)
            time.sleep(0.5)
            self.assertIsNotNone(w.degraded)
            self.queue.down = None                                          # the quota is back
            self.assertTrue(self.wait_status(task["id"], "completed", 5.0))
            time.sleep(0.5)                                                 # later passes must not rerun it
        self.assertEqual(len(calls), 1)
        self.assertIsNone(w.degraded)


class ControlTests(Harness):
    def test_pause_and_cancel_still_work_with_the_doorbell_loop(self) -> None:
        def handler(platform, ctx, task, reporter):
            if task["params"].get("mode") == "pause":
                raise worker_mod.TaskPaused({"page": 4820})
            raise worker_mod.TaskCancelled()

        with mock.patch.object(worker_mod, "resolve_handler", return_value=handler):
            w = worker_mod.PlatformWorker(self.platform, worker_id="w1", maintenance_seconds=60)
            w.maintain = lambda: {}
            self.start(w)
            paused = self.platform.tasks.submit(self.ctx, "analytics", {"mode": "pause"})
            cancelled = self.platform.tasks.submit(self.ctx, "analytics", {"mode": "cancel"})
            self.assertTrue(self.wait_status(paused["id"], "paused"))
            self.assertTrue(self.wait_status(cancelled["id"], "cancelled"))
        kept = self.platform.tasks.get(self.ctx, paused["id"])
        self.assertEqual((kept["progress"]["checkpoint"], kept["attempts"]), ({"page": 4820}, 0))


class RedisDoorbellTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import fakeredis
        except ImportError:  # pragma: no cover
            self.skipTest("fakeredis not installed")
        self.client = fakeredis.FakeRedis(decode_responses=True)
        self.queue = RedisJobQueue(self.client, prefix=f"test:{uuid.uuid4().hex}")

    def test_immediate_work_rings_the_bounded_doorbell_and_delayed_work_does_not(self) -> None:
        self.queue.enqueue("ws/a", delay_seconds=30)
        self.assertEqual(self.client.llen(self.queue._keys["bell"]), 0)
        self.assertFalse(self.queue.wait_for_work(1))                     # nothing rang: times out
        for i in range(100):
            self.queue.enqueue(f"ws/t{i}")
        self.assertEqual(self.client.llen(self.queue._keys["bell"]), 64)  # capped
        self.assertTrue(self.queue.wait_for_work(1))
        delivery = self.queue.reserve("w1", visibility_timeout=60)
        self.assertEqual(delivery.job_id, "ws/t0")                        # FIFO order untouched
        self.assertFalse(self.queue.enqueue("ws/t1"))                     # still de-duplicated


if __name__ == "__main__":
    unittest.main()
