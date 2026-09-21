"""The ``/status`` endpoint: does it tell the truth about each moving part?

``/health`` says the API process answered. ``/status`` is the one that actually
touches PostgreSQL and Redis, counts the queue and looks for a live worker, and
it is what puts "Crawler worker offline" on the dashboard. These tests drive it
through HTTP with backends that are deliberately broken one at a time.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from cloud.api.auth import DevTokenIssuer
from cloud.api.main import create_app
from cloud.api.settings import Settings
from cloud.shared.queue import InMemoryJobQueue, JobQueue, QueueStats, WorkerPresence
from cloud.shared.repository import InMemoryJobRepository
from cloud.shared.storage import LocalFileStorage
from cloud.worker.dispatcher import NullDispatcher

TEST_SECRET = "phase-5d-status-tests-secret-0123456789abcdef"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class RedisLikeQueue(InMemoryJobQueue):
    """An in-memory queue that claims to be Redis.

    ``/status`` treats a queue named "redis" as a real external queue, and only
    then looks for workers. Borrowing the name exercises every branch without
    needing a Redis.
    """

    name = "redis"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pingable = True

    def ping(self) -> None:
        if not self.pingable:
            raise ConnectionError("queue is unreachable")


class BrokenRepository(InMemoryJobRepository):
    """A repository that claims to be PostgreSQL and cannot be reached.

    The name matters: ``/status`` only pings a repository that is supposed to be
    a real database. An in-memory one is reported as ``disabled`` without a
    round trip, which is the correct answer in local development.
    """

    name = "postgres"

    def ping(self) -> None:
        raise ConnectionError("could not connect to server: port 5432 refused")


class _StatusTest(unittest.TestCase):
    def build(self, *, repository=None, queue: JobQueue = None, **kwargs) -> TestClient:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        issuer = DevTokenIssuer(TEST_SECRET)
        settings = Settings(
            fake_step_seconds=0,
            auth_mode="dev",
            results_dir=Path(scratch.name) / "results",
            **kwargs,
        )
        app = create_app(
            settings,
            repository=repository if repository is not None else InMemoryJobRepository(),
            queue=queue,
            dispatcher=None if queue is not None else NullDispatcher(),
            storage=LocalFileStorage(Path(scratch.name) / "results"),
            token_verifier=issuer,
        )
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {issuer.issue('op@example.com')['access_token']}"
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def status(self, client: TestClient) -> dict:
        response = client.get("/api/v1/status")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class TestStatusRequiresAuth(_StatusTest):
    def test_anonymous_callers_are_refused(self) -> None:
        """Queue depth and worker presence are not public information."""
        client = self.build()
        client.headers.pop("Authorization")
        self.assertEqual(client.get("/api/v1/status").status_code, 401)

    def test_health_stays_public(self) -> None:
        client = self.build()
        client.headers.pop("Authorization")
        self.assertEqual(client.get("/api/v1/health").status_code, 200)


class TestComponentReporting(_StatusTest):
    def test_everything_up_is_ok(self) -> None:
        body = self.status(self.build(queue=RedisLikeQueue(clock=Clock())))
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["api"]["status"], "ok")
        self.assertEqual(body["redis"]["status"], "ok")
        self.assertEqual(body["environment"], "development")
        self.assertIn("checked_at", body)

    def test_an_unreachable_database_is_degraded_not_a_500(self) -> None:
        """A status endpoint that dies with its backend is useless."""
        body = self.status(self.build(repository=BrokenRepository(), queue=RedisLikeQueue(clock=Clock())))
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["database"]["status"], "down")
        self.assertIn("5432", body["database"]["detail"])
        # The API itself is still fine, and says so.
        self.assertEqual(body["api"]["status"], "ok")

    def test_an_unreachable_queue_is_degraded(self) -> None:
        queue = RedisLikeQueue(clock=Clock())
        queue.pingable = False
        body = self.status(self.build(queue=queue))
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["redis"]["status"], "down")

    def test_a_failure_detail_carries_no_credentials(self) -> None:
        class LeakyRepository(InMemoryJobRepository):
            name = "postgres"

            def ping(self) -> None:
                raise ConnectionError("connection to host failed")

        body = self.status(self.build(repository=LeakyRepository(), queue=RedisLikeQueue(clock=Clock())))
        detail = body["database"]["detail"].lower()
        self.assertNotIn("password", detail)
        self.assertNotIn("@", detail)

    def test_in_process_backends_report_disabled_not_broken(self) -> None:
        """Local development has no Postgres and no Redis, and that is fine."""
        body = self.status(self.build())
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["database"]["status"], "disabled")
        self.assertEqual(body["database"]["backend"], "memory")
        self.assertEqual(body["redis"]["status"], "disabled")


class TestWorkerPresence(_StatusTest):
    def test_no_worker_says_offline_with_an_actionable_message(self) -> None:
        worker = self.status(self.build(queue=RedisLikeQueue(clock=Clock())))["worker"]
        self.assertFalse(worker["online"])
        self.assertEqual(worker["count"], 0)
        self.assertIn("Crawler worker offline", worker["message"])
        self.assertIn("Start the worker", worker["message"])

    def test_a_beating_worker_is_online(self) -> None:
        queue = RedisLikeQueue(clock=Clock())
        queue.heartbeat_worker("worker-1")
        worker = self.status(self.build(queue=queue))["worker"]
        self.assertTrue(worker["online"])
        self.assertEqual(worker["count"], 1)
        self.assertIsNotNone(worker["last_heartbeat"])
        self.assertIn("online", worker["message"])

    def test_a_worker_that_stopped_beating_goes_offline(self) -> None:
        clock = Clock()
        queue = RedisLikeQueue(clock=clock)
        queue.heartbeat_worker("worker-1")
        clock.now += 600
        worker = self.status(self.build(queue=queue))["worker"]
        self.assertFalse(worker["online"])
        # It still reports when the worker was last seen.
        self.assertIsNotNone(worker["last_heartbeat"])
        self.assertGreater(worker["seconds_since_heartbeat"], 90)

    def test_queue_depth_is_reported(self) -> None:
        queue = RedisLikeQueue(clock=Clock())
        queue.enqueue("job_a")
        queue.enqueue("job_b")
        queue.enqueue("job_later", delay_seconds=60)
        body = self.status(self.build(queue=queue))
        self.assertEqual(body["queue"]["ready"], 2)
        self.assertEqual(body["queue"]["delayed"], 1)

    def test_offline_with_queued_work_says_how_much_is_waiting(self) -> None:
        queue = RedisLikeQueue(clock=Clock())
        queue.enqueue("job_a")
        queue.enqueue("job_b")
        worker = self.status(self.build(queue=queue))["worker"]
        self.assertFalse(worker["online"])
        self.assertIn("2 crawls queued", worker["message"])

    def test_one_queued_crawl_reads_as_singular(self) -> None:
        queue = RedisLikeQueue(clock=Clock())
        queue.enqueue("job_a")
        self.assertIn("1 crawl queued", self.status(self.build(queue=queue))["worker"]["message"])

    def test_an_unreachable_queue_does_not_claim_the_worker_is_offline(self) -> None:
        """Not knowing is different from knowing it is down."""
        queue = RedisLikeQueue(clock=Clock())
        queue.heartbeat_worker("worker-1")
        queue.pingable = False
        worker = self.status(self.build(queue=queue))["worker"]
        self.assertIn("Cannot tell", worker["message"])

    def test_inline_mode_reports_the_api_as_the_worker(self) -> None:
        """With no external queue the API runs jobs itself; there is nothing to start."""
        worker = self.status(self.build())["worker"]
        self.assertTrue(worker["online"])
        self.assertIn("API process", worker["message"])

    def test_presence_failure_is_reported_not_raised(self) -> None:
        class AngryQueue(RedisLikeQueue):
            def worker_presence(self, *, stale_after: float = 90.0) -> WorkerPresence:
                raise RuntimeError("WRONGTYPE Operation against a key")

        worker = self.status(self.build(queue=AngryQueue(clock=Clock())))["worker"]
        self.assertFalse(worker["online"])
        self.assertIn("Cannot tell", worker["message"])

    def test_the_staleness_threshold_is_configurable(self) -> None:
        clock = Clock()
        queue = RedisLikeQueue(clock=clock)
        queue.heartbeat_worker("worker-1")
        clock.now += 120  # past the 90 s default, inside a 300 s threshold
        body = self.status(self.build(queue=queue, worker_stale_after_seconds=300.0))
        self.assertTrue(body["worker"]["online"])
        self.assertEqual(body["worker"]["stale_after_seconds"], 300.0)


class TestStatsShape(unittest.TestCase):
    def test_waiting_is_ready_plus_delayed(self) -> None:
        self.assertEqual(QueueStats(ready=2, delayed=3, in_flight=9).waiting, 5)


if __name__ == "__main__":
    unittest.main()
