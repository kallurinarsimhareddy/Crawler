"""Every registered task kind on the PostgreSQL queue + worker: it has a handler, can be
enqueued, reserved and executed; a crash is retried with backoff; cancellation, permanent
failure and workspace isolation hold; status and errors are visible over the API and only
writers may act on tasks. Four kinds with no feature-suite coverage run their real handlers
(no outbound email, no network, no paid provider)."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from cloud.intel.core.context import Ctx, utcnow
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.intel.store.spec import TASK_KINDS
from cloud.intel.tasks import worker as worker_mod
from cloud.intel.tasks.pgqueue import PostgresTaskQueue


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self):
        return self.now


class KindMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.user = str(uuid.uuid4())
        ws = self.store.create_workspace(self.user, "A", f"a-{uuid.uuid4().hex[:6]}")
        self.ctx = Ctx(ws["id"], self.user, "owner")
        self.clock = Clock()
        self.queue = PostgresTaskQueue(self.store, clock=self.clock)
        self.platform = Platform(self.store, queue=self.queue, config=PlatformConfig())
        self.platform.tasks._clock = self.clock

    def run_one(self, task_id):
        self.queue._recent.clear()
        q = PostgresTaskQueue(self.store, clock=self.clock, only_ids=[task_id])
        w = worker_mod.PlatformWorker(self.platform, worker_id="matrix", lease_seconds=30)
        w.queue = q
        return w.process_next()

    def test_every_kind_has_a_working_handler(self) -> None:
        self.assertEqual(len(TASK_KINDS), len(worker_mod.HANDLERS))
        for kind in TASK_KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(callable(worker_mod.resolve_handler(kind)))

    def test_every_kind_enqueues_reserves_retries_cancels_and_fails_cleanly(self) -> None:
        for kind in TASK_KINDS:
            with self.subTest(kind=kind):
                with mock.patch.object(worker_mod, "resolve_handler",
                                       return_value=lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))):
                    task = self.platform.tasks.submit(self.ctx, kind)
                    self.assertIn(f"{self.ctx.workspace_id}/{task['id']}", self.queue.due_keys(limit=100))
                    self.assertTrue(self.run_one(task["id"]))
                    retrying = self.platform.tasks.get(self.ctx, task["id"])
                    self.assertEqual((retrying["status"], retrying["attempts"]), ("retrying", 1))
                    self.assertIn("boom", retrying["error"])
                    self.assertGreater(retrying["run_after"], self.clock.now)      # backoff
                    self.assertNotIn(task["id"], " ".join(self.queue.due_keys(limit=100)))
                    self.platform.tasks.cancel(self.ctx, task["id"])
                    self.assertEqual(self.platform.tasks.get(self.ctx, task["id"])["status"], "cancelled")
                with mock.patch.object(worker_mod, "resolve_handler",
                                       return_value=lambda *a: (_ for _ in ()).throw(worker_mod.PermanentTaskError("bad input"))):
                    bad = self.platform.tasks.submit(self.ctx, kind)
                    self.run_one(bad["id"])
                    self.assertEqual(self.platform.tasks.get(self.ctx, bad["id"])["status"], "failed")

    def test_every_kind_runs_in_its_own_workspace_only(self) -> None:
        other = str(uuid.uuid4())
        ws_b = self.store.create_workspace(other, "B", f"b-{uuid.uuid4().hex[:6]}")
        seen = []
        with mock.patch.object(worker_mod, "resolve_handler",
                               return_value=lambda p, c, t, r: seen.append((t["kind"], c.workspace_id)) or {}):
            for kind in TASK_KINDS:
                task = self.platform.tasks.submit(Ctx(ws_b["id"], other, "owner"), kind)
                self.run_one(task["id"])
        self.assertEqual({ws for _, ws in seen}, {ws_b["id"]})
        self.assertEqual(len(seen), len(TASK_KINDS))

    def test_kinds_without_feature_coverage_run_their_real_handlers_safely(self) -> None:
        cases = {"discovery": {"source": "urls", "urls": []}, "email_send": {}, "scoring": {},
                 "integration": {"event": "notification", "payload": {}}}
        for kind, params in cases.items():
            with self.subTest(kind=kind):
                task = self.platform.tasks.submit(self.ctx, kind, params)
                self.assertTrue(self.run_one(task["id"]))
                done = self.platform.tasks.get(self.ctx, task["id"])
                self.assertEqual(done["status"], "completed", done.get("error"))
        sent = self.platform.tasks.get(self.ctx, self.store.first(
            self.ctx, "platform_tasks", {"kind": "email_send"})["id"])
        self.assertIn("nothing was sent", sent["result"]["note"])                   # never sends in tests


class TaskApiTests(unittest.TestCase):
    """Status / error visibility and who may act on a task, over HTTP."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from cloud.api.auth import DevTokenIssuer
        from cloud.api.main import create_app
        from cloud.api.settings import Settings
        from cloud.shared.storage import LocalFileStorage
        from cloud.worker.dispatcher import NullDispatcher

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.store = MemoryStore()
        self.queue = PostgresTaskQueue(self.store)
        self.platform = Platform(self.store, queue=self.queue, config=PlatformConfig())
        self.issuer = DevTokenIssuer("task-matrix-test-secret-0123456789abcdef")
        app = create_app(Settings(auth_mode="dev", results_dir=root / "results"), storage=LocalFileStorage(root),
                         token_verifier=self.issuer, dispatcher=NullDispatcher(), platform=self.platform)

        def client(email):
            c = TestClient(app)
            c.headers["Authorization"] = f"Bearer {self.issuer.issue(email)['access_token']}"
            return c

        self.owner, self.stranger = client("own@example.com"), client("x@example.com")
        self.ws = self.owner.post("/api/v1/workspaces", json={"name": "T", "seed": False}).json()["id"]
        self.base = f"/api/v1/w/{self.ws}"
        owner_ctx_user = next(m["user_id"] for m in self.owner.get(self.base + "/members").json()["items"])
        self.ctx = Ctx(self.ws, owner_ctx_user, "owner")

    def test_status_and_errors_are_visible_and_only_writers_act(self) -> None:
        with mock.patch.object(worker_mod, "resolve_handler",
                               return_value=lambda *a: (_ for _ in ()).throw(RuntimeError("upstream timeout"))):
            task = self.platform.tasks.submit(self.ctx, "export")
            worker_mod.PlatformWorker(self.platform, worker_id="api").process_next()
        body = self.owner.get(self.base + f"/tasks/{task['id']}").json()
        self.assertEqual(body["status"], "retrying")
        self.assertIn("upstream timeout", body["error"])
        self.assertIn("Retrying", body["progress"]["message"])
        listed = self.owner.get(self.base + "/tasks").json()["items"]
        self.assertIn(task["id"], [t["id"] for t in listed])
        # another account sees nothing and cannot act
        self.assertIn(self.stranger.get(self.base + f"/tasks/{task['id']}").status_code, (403, 404))
        self.assertIn(self.stranger.post(self.base + f"/tasks/{task['id']}/cancel").status_code, (403, 404))
        r = self.owner.post(self.base + f"/tasks/{task['id']}/cancel")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
