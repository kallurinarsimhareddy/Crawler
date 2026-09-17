"""The queue contract, for the in-memory queue and the Redis queue.

The Redis queue's Lua scripts run against ``fakeredis`` (with a real Lua
interpreter). Set ``CAREERCLOUD_TEST_REDIS_URL`` to also run them against a real,
disposable Redis — the tests use a random key prefix and delete their keys.
"""

from __future__ import annotations

import os
import threading
import unittest
import uuid

from cloud.shared.queue import InMemoryJobQueue, JobQueue, RedisJobQueue


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class QueueContract:
    queue: JobQueue
    clock: Clock

    def test_fifo(self) -> None:
        for name in ("a", "b", "c"):
            self.assertTrue(self.queue.enqueue(name))
        self.assertEqual([self.queue.reserve("w", visibility_timeout=60).job_id for _ in range(3)], ["a", "b", "c"])
        self.assertIsNone(self.queue.reserve("w", visibility_timeout=60))

    def test_enqueue_is_idempotent_while_waiting(self) -> None:
        self.assertTrue(self.queue.enqueue("a"))
        self.assertFalse(self.queue.enqueue("a"))
        self.assertFalse(self.queue.enqueue("a", delay_seconds=30))
        self.assertEqual(self.queue.stats().ready, 1)
        delivery = self.queue.reserve("w", visibility_timeout=60)
        # once taken, it may be enqueued again (e.g. a retry scheduled mid-run)
        self.assertTrue(self.queue.enqueue("a", delay_seconds=10))
        self.queue.ack(delivery)
        self.assertEqual(self.queue.stats().delayed, 1)

    def test_delayed_ids_wait_their_turn(self) -> None:
        self.queue.enqueue("later", delay_seconds=30)
        self.queue.enqueue("now")
        self.assertEqual(self.queue.reserve("w", visibility_timeout=60).job_id, "now")
        self.assertIsNone(self.queue.reserve("w", visibility_timeout=60))
        self.clock.now += 31
        self.assertEqual(self.queue.reserve("w", visibility_timeout=60).job_id, "later")

    def test_unacknowledged_deliveries_come_back_after_the_timeout(self) -> None:
        self.queue.enqueue("a")
        first = self.queue.reserve("w1", visibility_timeout=60)
        self.assertEqual(first.delivery_count, 1)
        self.clock.now += 30
        self.assertIsNone(self.queue.reserve("w2", visibility_timeout=60))
        self.clock.now += 31
        second = self.queue.reserve("w2", visibility_timeout=60)
        self.assertEqual((second.job_id, second.delivery_count), ("a", 2))
        self.queue.ack(second)
        self.assertEqual(self.queue.stats().in_flight, 0)

    def test_extend_keeps_a_delivery_hidden(self) -> None:
        self.queue.enqueue("a")
        delivery = self.queue.reserve("w", visibility_timeout=60)
        self.clock.now += 50
        self.assertTrue(self.queue.extend(delivery, visibility_timeout=60))
        self.clock.now += 50
        self.assertIsNone(self.queue.reserve("w2", visibility_timeout=60))
        self.queue.ack(delivery)
        self.assertFalse(self.queue.extend(delivery, visibility_timeout=60))

    def test_ack_forgets_and_stats_count(self) -> None:
        self.queue.enqueue("a")
        self.queue.enqueue("b", delay_seconds=5)
        delivery = self.queue.reserve("w", visibility_timeout=60)
        stats = self.queue.stats()
        self.assertEqual((stats.ready, stats.delayed, stats.in_flight), (0, 1, 1))
        self.queue.ack(delivery)
        self.assertEqual(self.queue.stats().in_flight, 0)
        self.clock.now += 100
        self.assertEqual(self.queue.reserve("w", visibility_timeout=60).job_id, "b")
        self.assertIsNone(self.queue.reserve("w", visibility_timeout=60))

    def test_concurrent_reservers_never_share_a_delivery(self) -> None:
        ids = [f"job{n}" for n in range(200)]
        for job_id in ids:
            self.queue.enqueue(job_id)
        taken, lock = [], threading.Lock()

        def drain(worker: str) -> None:
            while True:
                delivery = self.queue.reserve(worker, visibility_timeout=600)
                if delivery is None:
                    return
                with lock:
                    taken.append(delivery.job_id)

        threads = [threading.Thread(target=drain, args=(f"w{n}",)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(taken), sorted(ids))


class TestInMemoryQueue(QueueContract, unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.queue = InMemoryJobQueue(clock=self.clock)


class TestRedisQueueOnFakeRedis(QueueContract, unittest.TestCase):
    def setUp(self) -> None:
        try:
            import fakeredis
        except ImportError:  # pragma: no cover
            self.skipTest("fakeredis not installed")
        self.clock = Clock()
        self.client = fakeredis.FakeRedis(decode_responses=True)
        self.queue = RedisJobQueue(self.client, prefix=f"test:{uuid.uuid4().hex}", clock=self.clock)

    def test_keys_are_namespaced(self) -> None:
        self.queue.enqueue("a")
        self.assertTrue(all(key.startswith(self.queue.keys()[0].rsplit(":", 1)[0]) for key in self.client.keys("*")))

    def test_a_bad_prefix_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            RedisJobQueue(self.client, prefix="has space")


@unittest.skipUnless(os.environ.get("CAREERCLOUD_TEST_REDIS_URL"), "set CAREERCLOUD_TEST_REDIS_URL to test a real Redis")
class TestRedisQueueOnRealRedis(QueueContract, unittest.TestCase):
    def setUp(self) -> None:
        import redis

        self.clock = Clock()
        self.client = redis.Redis.from_url(os.environ["CAREERCLOUD_TEST_REDIS_URL"], decode_responses=True)
        self.queue = RedisJobQueue(self.client, prefix=f"careercloud-test:{uuid.uuid4().hex}", clock=self.clock)
        self.addCleanup(lambda: self.client.delete(*self.queue.keys()))


if __name__ == "__main__":
    unittest.main()
