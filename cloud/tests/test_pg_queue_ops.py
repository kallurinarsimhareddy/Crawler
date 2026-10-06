"""Operations with the PostgreSQL task queue: /status reports the platform worker (from its
state file) and queue depth (from the database) with no Redis; legacy crawl jobs stay
queued; the supervisor health check needs no Redis."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


class StatusOnPostgresQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "worker-state.json"

    def client(self):
        from fastapi.testclient import TestClient

        from cloud.api.main import create_app
        from cloud.api.settings import load_settings

        env = {"CAREERCLOUD_ENV": "development", "CAREERCLOUD_STORAGE": "memory", "CAREERCLOUD_QUEUE": "postgres",
               "CAREERCLOUD_AUTH_MODE": "dev", "CAREERCLOUD_DEV_JWT_SECRET": "x" * 40,
               "CAREERCLOUD_WORKER_STATE_FILE": str(self.state)}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.dict("sys.modules", {"redis": None}):
            app = create_app(load_settings(env))
        client = TestClient(app)
        token = client.post("/api/v1/auth/dev-session", json={"email": "ops@example.com"}).json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        return client, app

    def test_status_reports_the_platform_worker_and_depth_without_redis(self) -> None:
        self.state.write_text(json.dumps({"state": "ok", "updated_at": datetime.now(timezone.utc).isoformat()}))
        client, app = self.client()
        self.assertEqual(app.state.queue.name, "postgres")
        body = client.get("/api/v1/status").json()
        self.assertEqual(body["redis"]["status"], "disabled")
        self.assertIn("PostgreSQL", body["redis"]["detail"])
        self.assertTrue(body["worker"]["online"])
        self.assertIn("ready", body["queue"])
        health = client.get("/api/v1/health").json()
        self.assertEqual(health["queue"], "postgres")

    def test_a_silent_worker_reads_offline(self) -> None:
        self.state.write_text(json.dumps({"state": "ok", "updated_at": "2020-01-01T00:00:00+00:00"}))
        client, _ = self.client()
        self.assertFalse(client.get("/api/v1/status").json()["worker"]["online"])


class HealthcheckWithoutRedisTests(unittest.TestCase):
    def test_postgres_queue_needs_no_redis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "worker.env"
            env_file.write_text("CAREERCLOUD_DATABASE_URL=postgresql://x@127.0.0.1:1/x\n"
                                "CAREERCLOUD_QUEUE=postgres\n", encoding="utf-8")
            out = subprocess.run([sys.executable, str(ROOT / "deploy/windows/sana-gtm/healthcheck.py"), str(env_file)],
                                 capture_output=True, text=True, timeout=60)
        report = json.loads(out.stdout)
        self.assertNotIn("queue", report["errors"])                    # no Redis was contacted
        self.assertFalse(report["queue"])                              # the queue is the (unreachable) database
        self.assertIn("database", report["errors"])                    # the one dependency it checks


if __name__ == "__main__":
    unittest.main()
