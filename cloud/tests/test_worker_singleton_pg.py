"""One platform worker per database (phase 2): a PID file cannot stop a second worker after a
crash or a manual start, and two workers would both run the schedulers. Real PostgreSQL."""

from __future__ import annotations

import threading
import time
import unittest

from cloud.tests._pg import PostgresTestCase


class WorkerSingletonTests(PostgresTestCase):
    def connect(self, url):
        import psycopg

        return psycopg.connect(url, autocommit=True)

    def test_a_second_worker_waits_and_takes_over_when_the_first_stops(self) -> None:
        from cloud.intel.tasks.worker import acquire_worker_lock

        first = acquire_worker_lock(self.database_url, stop=threading.Event(), once=True)
        self.assertIsNotNone(first)
        self.assertIsNone(acquire_worker_lock(self.database_url, stop=threading.Event(), once=True))
        got = {}
        stop = threading.Event()
        standby = threading.Thread(target=lambda: got.setdefault(
            "conn", acquire_worker_lock(self.database_url, stop=stop, poll_seconds=0.2)))
        standby.start()
        time.sleep(0.6)
        self.assertNotIn("conn", got)                 # still on standby
        first.close()                                 # the active worker exits (or dies)
        standby.join(10)
        self.assertIsNotNone(got.get("conn"))
        got["conn"].close()

    def test_standby_stops_promptly_on_shutdown(self) -> None:
        from cloud.intel.tasks.worker import acquire_worker_lock

        holder = acquire_worker_lock(self.database_url, stop=threading.Event(), once=True)
        self.addCleanup(holder.close)
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        started = time.monotonic()
        self.assertIsNone(acquire_worker_lock(self.database_url, stop=stop, poll_seconds=5))
        self.assertLess(time.monotonic() - started, 3)

    def test_the_watch_retakes_a_dropped_lock_or_stops_the_worker(self) -> None:
        import psycopg

        from cloud.intel.tasks.worker import WorkerLockWatch, acquire_worker_lock

        def kill(conn):
            pid = conn.info.backend_pid
            with psycopg.connect(self.database_url, autocommit=True) as admin:
                admin.execute("select pg_terminate_backend(%s)", [pid])

        stopped = []
        lock = acquire_worker_lock(self.database_url, stop=threading.Event(), once=True)
        watch = WorkerLockWatch(lock, self.database_url, lambda: stopped.append(True))
        kill(watch.conn)                              # e.g. PostgreSQL restarted
        self.assertTrue(watch.check())                # re-taken on a new connection
        self.assertNotEqual(watch.conn, lock)
        kill(watch.conn)
        rival = acquire_worker_lock(self.database_url, stop=threading.Event(), once=True)   # standby wins
        self.addCleanup(rival.close)
        self.assertFalse(watch.check())               # this worker must stop
        watch.close()


class HealthProbeTests(unittest.TestCase):
    """Health fails when a dependency is down, not only when a process is gone."""

    def test_api_is_unhealthy_when_its_database_is_down(self) -> None:
        import json
        from unittest import mock

        from cloud.ops import selfhost

        class Response:
            status = 200

            def read(self):
                return json.dumps({"service": "careercloud-api"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        env = {"CAREERCLOUD_DATABASE_URL": "postgresql://u@127.0.0.1:1/x", "SANAGTM_API_PORT": "8100"}
        with mock.patch("urllib.request.urlopen", lambda *a, **k: Response()), \
                mock.patch("psycopg.connect", side_effect=OSError("refused")):
            result = selfhost.health_api(env)
        self.assertFalse(result["ok"])
        self.assertEqual(result["database_error"], "OSError")
        self.assertNotIn("postgresql://", json.dumps(result))     # no URL in the probe output
        ok = mock.MagicMock()
        ok.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (1,)
        with mock.patch("urllib.request.urlopen", lambda *a, **k: Response()), mock.patch("psycopg.connect", ok):
            self.assertTrue(selfhost.health_api(env)["ok"])

    def test_a_degraded_worker_is_unhealthy(self) -> None:
        import json
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path
        from unittest import mock

        from cloud.ops import selfhost

        ok = mock.MagicMock()
        ok.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (1,)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "worker-state.json"
            env = {"CAREERCLOUD_WORKER_STATE_FILE": str(state), "CAREERCLOUD_DATABASE_URL": "postgresql://x/y"}
            for value, healthy in (("ok", True), ("queue_unavailable", False)):
                state.write_text(json.dumps({"state": value, "updated_at": datetime.now(timezone.utc).isoformat()}))
                with self.subTest(state=value), mock.patch("psycopg.connect", ok):
                    self.assertEqual(selfhost.health_worker(env)["ok"], healthy)

    def test_the_cli_fails_closed_when_the_api_is_down(self) -> None:
        from unittest import mock

        from cloud.ops import selfhost

        with mock.patch.dict("os.environ", {"SANAGTM_API_PORT": "1", "CAREERCLOUD_DATABASE_URL": "postgresql://x/y"}):
            self.assertEqual(selfhost.main(["health", "api"]), 1)


if __name__ == "__main__":
    unittest.main()
