"""The PostgreSQL task queue on a real database: the cross-workspace due-task query, two
workers with separate connection pools racing for the same tasks (each runs exactly once),
LISTEN/NOTIFY wake-up, a paused sweep that is never touched, crash recovery, counts and
retention."""

from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from datetime import timedelta
from unittest import mock

from cloud.tests._pg import PostgresTestCase


class PostgresTaskQueueTests(PostgresTestCase):
    def setUp(self) -> None:
        from cloud.intel.core.context import Ctx
        from cloud.intel.platform import Platform, PlatformConfig
        from cloud.intel.store.postgres import PostgresStore
        from cloud.intel.tasks.pgqueue import PostgresTaskQueue

        self.Ctx, self.PostgresTaskQueue, self.Platform, self.PlatformConfig = Ctx, PostgresTaskQueue, Platform, PlatformConfig
        self.PostgresStore = PostgresStore
        self.store = PostgresStore.from_url(self.database_url, max_size=4)
        self.addCleanup(self.store.close)
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "Q", f"q-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.queue = PostgresTaskQueue(self.store, url=self.database_url)
        self.addCleanup(self.queue.close)
        self.platform = Platform(self.store, queue=self.queue, config=PlatformConfig())

    def mine(self, keys):
        return [k for k in keys if k.startswith(self.ctx.workspace_id)]

    def test_due_query_order_scheduling_counts_and_paused_tasks(self) -> None:
        from cloud.intel.core.context import utcnow

        tasks = self.platform.tasks
        bulk = tasks.submit(self.ctx, "job_lifecycle")
        interactive = tasks.submit(self.ctx, "enrichment")
        later = tasks.submit(self.ctx, "analytics", delay_seconds=3600)
        sweep = tasks.submit(self.ctx, "job_monitor")
        sys_ctx = self.Ctx.for_system(self.ctx.workspace_id)
        self.store.update(sys_ctx, "platform_tasks", sweep["id"],
                          {"status": "paused", "progress": {"checkpoint": {"page": 4820}, "message": "Paused"}})
        keys = self.mine(self.queue.due_keys(limit=20))
        self.assertEqual([k.split("/")[1] for k in keys], [interactive["id"], bulk["id"]])
        counts = self.store.task_queue_counts(now=utcnow())
        self.assertGreaterEqual(counts["ready"], 2)
        self.assertGreaterEqual(counts["delayed"], 1)
        self.assertNotIn(later["id"], " ".join(keys))
        kept = tasks.get(self.ctx, sweep["id"])
        self.assertEqual((kept["status"], kept["progress"]["checkpoint"]), ("paused", {"page": 4820}))

    def test_two_workers_with_separate_pools_run_each_task_exactly_once(self) -> None:
        from cloud.intel.tasks import worker as worker_mod

        ran = Counter()
        lock = threading.Lock()

        def work(platform, ctx, task, reporter):
            with lock:
                ran[task["id"]] += 1
            return {}

        submitted = [self.platform.tasks.submit(self.ctx, "analytics") for _ in range(40)]
        ids = [t["id"] for t in submitted]
        workers = []
        for n in range(2):
            store = self.PostgresStore.from_url(self.database_url, max_size=3)
            self.addCleanup(store.close)
            queue = self.PostgresTaskQueue(store, only_ids=ids)
            platform = self.Platform(store, queue=queue, config=self.PlatformConfig())
            workers.append(worker_mod.PlatformWorker(platform, worker_id=f"pg-w{n}", lease_seconds=60))

        def drain(w):
            idle = 0
            while idle < 15:
                if w.process_next():
                    idle = 0
                else:
                    idle += 1
                    w.queue._recent.clear()
                    time.sleep(0.01)

        with mock.patch.object(worker_mod, "resolve_handler", return_value=work):
            threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=120)
        self.assertEqual(set(ran), set(ids))
        self.assertEqual(set(ran.values()), {1})
        rows = [self.platform.tasks.get(self.ctx, i) for i in ids]
        self.assertEqual({r["status"] for r in rows}, {"completed"})
        self.assertEqual({r["attempts"] for r in rows}, {1})
        self.assertEqual({r["worker_id"] for r in rows}, {"pg-w0", "pg-w1"})   # both took work

    def test_listen_notify_wakes_an_idle_worker(self) -> None:
        listener = self.PostgresTaskQueue(self.store, url=self.database_url)
        self.addCleanup(listener.close)
        listener.wait_for_work(0.2)                                          # LISTEN is now active
        result = {}

        def wait():
            started = time.monotonic()
            result["woke"] = listener.wait_for_work(10)
            result["after"] = time.monotonic() - started

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.5)
        self.platform.tasks.submit(self.ctx, "analytics")                    # NOTIFY via the store
        thread.join(timeout=15)
        self.assertTrue(result["woke"])
        self.assertLess(result["after"], 2.0)
        started = time.monotonic()
        self.assertFalse(listener.wait_for_work(1))                          # nothing pending: times out
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    def test_crash_recovery_and_retention_on_postgres(self) -> None:
        from cloud.intel.core.context import utcnow
        from cloud.intel.tasks import worker as worker_mod

        sys_ctx = self.Ctx.for_system(self.ctx.workspace_id)
        task = self.platform.tasks.submit(self.ctx, "analytics")
        claimed = self.platform.tasks.claim(sys_ctx, task["id"], worker_id="A", lease_seconds=1)
        self.platform.tasks.heartbeat(sys_ctx, claimed, lease_seconds=1, progress={"checkpoint": {"page": 7}})
        self.assertNotIn(task["id"], " ".join(self.queue.due_keys(limit=50)))   # running: not offered
        time.sleep(1.2)
        self.platform.tasks.reap(sys_ctx)
        self.store.update(sys_ctx, "platform_tasks", task["id"], {"run_after": utcnow() - timedelta(seconds=1)})
        with mock.patch.object(worker_mod, "resolve_handler",
                               return_value=lambda p, c, t, r: {"from": r.checkpoint["page"]}):
            w = worker_mod.PlatformWorker(self.Platform(self.store, queue=self.PostgresTaskQueue(
                self.store, only_ids=[task["id"]]), config=self.PlatformConfig()), worker_id="B")
            self.assertTrue(w.process_next())
        done = self.platform.tasks.get(self.ctx, task["id"])
        self.assertEqual((done["status"], done["result"], done["worker_id"]), ("completed", {"from": 7}, "B"))
        # retention on the real table
        self.store.update(sys_ctx, "platform_tasks", task["id"], {"finished_at": utcnow() - timedelta(days=200)})
        self.assertEqual(self.platform.tasks.purge_finished(sys_ctx, older_than_days=90), 1)
        with self.assertRaises(Exception):
            self.platform.tasks.get(self.ctx, task["id"])

    def test_worker_from_bootstrap_with_postgres_queue_and_no_redis(self) -> None:
        from cloud.intel.bootstrap import build_platform
        from cloud.intel.tasks import worker as worker_mod

        env = {"CAREERCLOUD_ENV": "test", "CAREERCLOUD_STORAGE": "postgres", "CAREERCLOUD_QUEUE": "postgres",
               "CAREERCLOUD_DATABASE_URL": self.database_url, "CAREERCLOUD_DB_POOL_MAX": "3"}
        with mock.patch.dict("sys.modules", {"redis": None}):
            platform = build_platform(role="worker", env=env)
            self.addCleanup(platform.close)
            self.assertEqual(platform.queue.name, "postgres")
            task = self.platform.tasks.submit(self.ctx, "analytics")
            platform.queue._only = [task["id"]]
            with mock.patch.object(worker_mod, "resolve_handler", return_value=lambda *a: {"ok": 1}):
                self.assertTrue(worker_mod.PlatformWorker(platform, worker_id="boot").process_next())
        self.assertEqual(self.platform.tasks.get(self.ctx, task["id"])["status"], "completed")
