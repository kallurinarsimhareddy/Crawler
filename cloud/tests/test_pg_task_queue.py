"""The PostgreSQL task queue semantics (on the in-memory store; the same store contract the
PostgreSQL store implements — see test_pg_task_queue_pg for the real database): enqueue,
reserve, claims, retries with backoff, permanent failure, pause / cancel (never retried),
checkpoints, lease expiry and restart recovery, idempotency, workspace isolation,
priority, scheduled work, concurrent workers, retention and a worker with no Redis."""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from collections import Counter
from datetime import timedelta
from unittest import mock

from cloud.intel.core.context import Ctx, utcnow
from cloud.intel.platform import Platform
from cloud.intel.store.memory import MemoryStore
from cloud.intel.tasks import worker as worker_mod
from cloud.intel.tasks.pgqueue import PostgresTaskQueue


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "A", f"a-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.clock = Clock()
        self.queue = PostgresTaskQueue(self.store, clock=self.clock)
        self.platform = Platform(self.store, queue=self.queue)
        self.platform.tasks._clock = self.clock
        self.calls: list = []

    def worker(self, worker_id="w1", **kw):
        return worker_mod.PlatformWorker(self.platform, worker_id=worker_id, lease_seconds=30, **kw)

    def get(self, task, ctx=None):
        return self.platform.tasks.get(ctx or self.ctx, task["id"])

    def handler(self, fn=None):
        def run(platform, ctx, task, reporter):
            self.calls.append((ctx.workspace_id, task["id"], dict(reporter.checkpoint or {})))
            return fn(platform, ctx, task, reporter) if fn else {"ok": True}
        return mock.patch.object(worker_mod, "resolve_handler", return_value=run)


class QueueSemanticsTests(Base):
    def test_enqueue_reserve_claim_complete(self) -> None:
        with self.handler():
            task = self.platform.tasks.submit(self.ctx, "analytics")
            self.assertEqual(self.queue.due_keys(), [f"{self.ctx.workspace_id}/{task['id']}"])
            self.assertTrue(self.worker().process_next())
        done = self.get(task)
        self.assertEqual((done["status"], done["attempts"], done["result"]), ("completed", 1, {"ok": True}))
        self.assertEqual(self.queue.due_keys(), [])
        self.assertEqual(self.queue.stats().ready, 0)

    def test_scheduled_work_waits_until_due(self) -> None:
        with self.handler():
            task = self.platform.tasks.submit(self.ctx, "analytics", delay_seconds=600)
            w = self.worker()
            self.assertFalse(w.process_next())
            self.assertEqual(self.queue.stats().delayed, 1)
            self.clock.advance(601)
            self.queue._recent.clear()
            self.assertTrue(w.process_next())
        self.assertEqual(self.get(task)["status"], "completed")

    def test_retry_backoff_then_success_and_permanent_failure(self) -> None:
        attempts = []

        def flaky(platform, ctx, task, reporter):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("network down")
            return {"ok": len(attempts)}

        with self.handler(flaky):
            task = self.platform.tasks.submit(self.ctx, "analytics")
            w = self.worker()
            w.process_next()
            retrying = self.get(task)
            self.assertEqual((retrying["status"], retrying["attempts"]), ("retrying", 1))
            self.assertGreater(retrying["run_after"], self.clock.now)     # exponential backoff
            self.queue._recent.clear()
            self.assertFalse(w.process_next())                              # not before it is due
            self.clock.advance(31)
            self.queue._recent.clear()
            self.assertTrue(w.process_next())
        self.assertEqual((self.get(task)["status"], self.get(task)["attempts"]), ("completed", 2))

        def broken(platform, ctx, task, reporter):
            raise worker_mod.PermanentTaskError("missing credentials")

        with self.handler(broken):
            bad = self.platform.tasks.submit(self.ctx, "analytics")
            self.queue._recent.clear()
            w.process_next()
        self.assertEqual(self.get(bad)["status"], "failed")
        self.queue._recent.clear()
        self.assertNotIn(bad["id"], " ".join(self.queue.due_keys()))

    def test_pause_keeps_checkpoint_never_retries_and_resume_continues(self) -> None:
        def pausing(platform, ctx, task, reporter):
            if not reporter.checkpoint:
                raise worker_mod.TaskPaused({"page": 4820})
            return {"resumed_from": reporter.checkpoint["page"]}

        with self.handler(pausing):
            task = self.platform.tasks.submit(self.ctx, "analytics")
            w = self.worker()
            w.process_next()
            paused = self.get(task)
            self.assertEqual((paused["status"], paused["attempts"]), ("paused", 0))
            self.assertEqual(paused["progress"]["checkpoint"], {"page": 4820})
            self.queue._recent.clear()
            self.assertEqual(self.queue.due_keys(), [])                    # a paused task is never reserved
            self.clock.advance(3600)
            self.assertFalse(w.process_next())
            self.platform.tasks.resume(self.ctx, task["id"])
            self.queue._recent.clear()
            self.assertTrue(w.process_next())
        self.assertEqual(self.get(task)["result"], {"resumed_from": 4820})

    def test_cancel_queued_never_runs_and_cancel_running_is_not_retried(self) -> None:
        def cancelling(platform, ctx, task, reporter):
            raise worker_mod.TaskCancelled()

        with self.handler(cancelling):
            queued = self.platform.tasks.submit(self.ctx, "analytics")
            self.platform.tasks.cancel(self.ctx, queued["id"])
            running = self.platform.tasks.submit(self.ctx, "analytics")
            w = self.worker()
            w.process_next()
        self.assertEqual(self.get(queued)["status"], "cancelled")
        self.assertEqual([c[1] for c in self.calls], [running["id"]])   # the cancelled one never ran
        self.assertEqual((self.get(running)["status"], self.get(running)["attempts"]), ("cancelled", 1))
        self.queue._recent.clear()
        self.assertEqual(self.queue.due_keys(), [])

    def test_worker_crash_lease_expiry_recovery_from_checkpoint(self) -> None:
        task = self.platform.tasks.submit(self.ctx, "analytics")
        # worker A claims, reports a checkpoint, then dies (no finish, no heartbeat)
        sys_ctx = Ctx.for_system(self.ctx.workspace_id)                      # how the worker acts
        claimed = self.platform.tasks.claim(sys_ctx, task["id"], worker_id="A", lease_seconds=30)
        self.platform.tasks.heartbeat(sys_ctx, claimed, lease_seconds=30, progress={"checkpoint": {"page": 12}})
        self.queue._recent.clear()
        self.assertEqual(self.queue.due_keys(), [])                         # running: nobody else takes it
        self.clock.advance(31)                                              # lease expires
        recovered = self.platform.tasks.reap(Ctx.for_system(self.ctx.workspace_id))
        self.assertEqual([r["status"] for r in recovered], ["retrying"])
        self.clock.advance(60)                                              # its backoff
        with self.handler(lambda p, c, t, r: {"from": r.checkpoint["page"]}):
            self.queue._recent.clear()
            self.assertTrue(self.worker("B").process_next())
        done = self.get(task)
        self.assertEqual((done["status"], done["result"], done["worker_id"]), ("completed", {"from": 12}, "B"))

    def test_idempotency_and_duplicate_delivery(self) -> None:
        with self.handler():
            a = self.platform.tasks.submit(self.ctx, "analytics", idempotency_key="k1")
            b = self.platform.tasks.submit(self.ctx, "analytics", idempotency_key="k1")
            self.assertEqual(a["id"], b["id"])
            w1, w2 = self.worker("w1"), self.worker("w2")
            w1.process_next()
            self.queue._recent.clear()
            w2.process_next()                                               # nothing left / not claimable
        self.assertEqual(len(self.calls), 1)

    def test_workspace_isolation(self) -> None:
        other_user = str(uuid.uuid4())
        ws_b = self.store.create_workspace(other_user, "B", f"b-{uuid.uuid4().hex[:6]}")
        ctx_b = Ctx(ws_b["id"], other_user, "owner")
        with self.handler():
            ta = self.platform.tasks.submit(self.ctx, "analytics")
            tb = self.platform.tasks.submit(ctx_b, "analytics")
            w = self.worker()
            while w.process_next():
                pass
        ran = {task_id: ws for ws, task_id, _ in self.calls}
        self.assertEqual(ran, {ta["id"]: self.ctx.workspace_id, tb["id"]: ctx_b.workspace_id})
        with self.assertRaises(Exception):
            self.platform.tasks.get(ctx_b, ta["id"])                        # B cannot read A's task

    def test_interactive_work_goes_before_bulk_runs_and_priority_overrides(self) -> None:
        bulk = self.platform.tasks.submit(self.ctx, "job_lifecycle")
        self.clock.advance(1)
        interactive = self.platform.tasks.submit(self.ctx, "enrichment")
        self.clock.advance(1)
        urgent_bulk = self.platform.tasks.submit(self.ctx, "job_monitor", {"priority": -1})
        order = [k.split("/")[1] for k in self.queue.due_keys()]
        self.assertEqual(order, [interactive["id"], urgent_bulk["id"], bulk["id"]])

    def test_an_unclaimable_task_is_not_offered_in_a_tight_loop(self) -> None:
        task = self.platform.tasks.submit(self.ctx, "analytics")
        with mock.patch.object(self.platform.tasks, "claim", return_value=None) as claim:
            w = self.worker()
            for _ in range(20):
                w.process_next()
            self.assertEqual(claim.call_count, 1)                           # cooldown, not 20 attempts
        self.assertEqual(self.get(task)["status"], "queued")

    def test_concurrent_workers_never_run_a_task_twice(self) -> None:
        ran = Counter()
        lock = threading.Lock()

        def work(platform, ctx, task, reporter):
            with lock:
                ran[task["id"]] += 1
            time.sleep(0.002)
            return {}

        with mock.patch.object(worker_mod, "resolve_handler", return_value=work):
            tasks = [self.platform.tasks.submit(self.ctx, "analytics") for _ in range(40)]
            queues = [PostgresTaskQueue(self.store, clock=self.clock) for _ in range(4)]
            workers = []
            for n, q in enumerate(queues):
                w = worker_mod.PlatformWorker(self.platform, worker_id=f"w{n}", lease_seconds=30)
                w.queue = q
                workers.append(w)

            def drain(w):
                idle = 0
                while idle < 30:
                    idle = 0 if w.process_next() else idle + 1
                    if idle:
                        w.queue._recent.clear()

            threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        self.assertEqual(set(ran), {t["id"] for t in tasks})
        self.assertEqual(set(ran.values()), {1})                            # exactly once each
        owners = {self.get(t)["worker_id"] for t in tasks}
        self.assertGreater(len(owners), 1)                                  # the work was shared

    def test_retention_purges_only_old_finished_tasks(self) -> None:
        sys_ctx = Ctx.for_system(self.ctx.workspace_id)
        old = self.platform.tasks.submit(self.ctx, "analytics")
        paused = self.platform.tasks.submit(self.ctx, "job_monitor")
        queued = self.platform.tasks.submit(self.ctx, "analytics")
        self.store.update(sys_ctx, "platform_tasks", old["id"],
                          {"status": "completed", "finished_at": self.clock.now - timedelta(days=120)})
        self.store.update(sys_ctx, "platform_tasks", paused["id"],
                          {"status": "paused", "progress": {"checkpoint": {"page": 4820}}})
        self.assertEqual(self.platform.tasks.purge_finished(sys_ctx, older_than_days=90), 1)
        self.assertEqual(self.platform.tasks.purge_finished(sys_ctx, older_than_days=0), 0)
        remaining = {t["id"]: t["status"] for t in self.store.rows(sys_ctx, "platform_tasks", {}, limit=50)}
        self.assertEqual(remaining, {paused["id"]: "paused", queued["id"]: "queued"})
        self.assertEqual(self.get(paused)["progress"]["checkpoint"], {"page": 4820})


class WorkerTests(Base):
    def test_doorbell_wakes_an_idle_worker_and_housekeeping_finds_delayed_work(self) -> None:
        with self.handler():
            w = self.worker(maintenance_seconds=60)
            w.maintain = lambda: {}
            thread = threading.Thread(target=w.run, daemon=True)
            thread.start()
            time.sleep(0.3)
            started = time.monotonic()
            task = self.platform.tasks.submit(self.ctx, "analytics")
            while self.get(task)["status"] != "completed" and time.monotonic() - started < 3:
                time.sleep(0.01)
            latency = time.monotonic() - started
            w.stop()
            thread.join(timeout=5)
        self.assertEqual(self.get(task)["status"], "completed")
        self.assertLess(latency, 0.5)
        self.assertEqual(w.housekeeping_seconds, PostgresTaskQueue.housekeeping_seconds)

    def test_durable_queue_skips_orphan_re_ringing(self) -> None:
        w = self.worker()
        with mock.patch.object(self.platform.tasks, "requeue_orphans") as requeue:
            w.maintain()
            w._last_orphans -= worker_mod.ORPHAN_SECONDS
            w.maintain()
        requeue.assert_not_called()

    def test_graceful_shutdown_releases_nothing_mid_queue(self) -> None:
        with self.handler():
            w = self.worker(maintenance_seconds=60)
            w.maintain = lambda: {}
            thread = threading.Thread(target=w.run, daemon=True)
            thread.start()
            time.sleep(0.2)
            w.stop()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            task = self.platform.tasks.submit(self.ctx, "analytics")       # after shutdown: stays queued
        self.assertEqual(self.get(task)["status"], "queued")

    def test_database_unavailable_idles_the_worker(self) -> None:
        import psycopg

        w = self.worker()
        with mock.patch.object(self.store, "due_tasks", side_effect=psycopg.OperationalError("server closed")):
            self.assertFalse(w.process_next())
        self.assertIsNotNone(w.degraded)
        self.assertIn("database is unreachable", w.degraded.reason)
        self.assertFalse(w.degraded.quota)

    def test_platform_builds_and_works_with_no_redis_at_all(self) -> None:
        from cloud.intel.bootstrap import build_platform

        env = {"CAREERCLOUD_STORAGE": "memory", "CAREERCLOUD_QUEUE": "postgres",
               "CAREERCLOUD_REDIS_URL": "redis://127.0.0.1:1/0"}          # unreachable: must not matter
        with mock.patch.dict("sys.modules", {"redis": None}):              # redis cannot even be imported
            platform = build_platform(role="worker", env=env)
            self.assertEqual(platform.queue.name, "postgres")
            user = str(uuid.uuid4())
            ws = platform.store.create_workspace(user, "W", f"w-{uuid.uuid4().hex[:6]}")
            ctx = Ctx(ws["id"], user, "owner")
            with mock.patch.object(worker_mod, "resolve_handler", return_value=lambda *a: {"ok": 1}):
                task = platform.tasks.submit(ctx, "analytics")
                self.assertTrue(worker_mod.PlatformWorker(platform, worker_id="w").process_next())
            self.assertEqual(platform.tasks.get(ctx, task["id"])["status"], "completed")


if __name__ == "__main__":
    unittest.main()
