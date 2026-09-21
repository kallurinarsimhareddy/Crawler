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


    # --- worker presence -----------------------------------------------------
    #
    # Presence is what tells the dashboard "Crawler worker offline". It is a
    # heartbeat with a staleness threshold, not a connection, so every case
    # below is about time passing rather than about sockets.

    def test_no_workers_have_ever_beaten(self) -> None:
        presence = self.queue.worker_presence(stale_after=90)
        self.assertEqual(presence.online, 0)
        self.assertIsNone(presence.last_heartbeat)
        self.assertIsNone(presence.seconds_since_heartbeat)
        self.assertFalse(presence.any_online)

    def test_a_beating_worker_is_online(self) -> None:
        self.queue.heartbeat_worker("worker-1")
        presence = self.queue.worker_presence(stale_after=90)
        self.assertEqual(presence.online, 1)
        self.assertTrue(presence.any_online)
        self.assertAlmostEqual(presence.seconds_since_heartbeat, 0.0, places=3)

    def test_several_workers_are_counted(self) -> None:
        for name in ("w1", "w2", "w3"):
            self.queue.heartbeat_worker(name)
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 3)

    def test_heartbeating_twice_does_not_double_count(self) -> None:
        self.queue.heartbeat_worker("w1")
        self.clock.now += 5
        self.queue.heartbeat_worker("w1")
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 1)

    def test_a_worker_goes_offline_once_its_heartbeat_is_stale(self) -> None:
        self.queue.heartbeat_worker("w1")
        self.clock.now += 89
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 1)
        self.clock.now += 2  # now 91s old
        presence = self.queue.worker_presence(stale_after=90)
        self.assertEqual(presence.online, 0)
        self.assertFalse(presence.any_online)
        # The timestamp survives: the dashboard says how long ago it was seen.
        self.assertIsNotNone(presence.last_heartbeat)
        self.assertAlmostEqual(presence.seconds_since_heartbeat, 91.0, places=3)

    def test_a_stale_worker_that_beats_again_comes_back_online(self) -> None:
        self.queue.heartbeat_worker("w1")
        self.clock.now += 600
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 0)
        self.queue.heartbeat_worker("w1")
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 1)

    def test_forgetting_a_worker_takes_it_offline_at_once(self) -> None:
        self.queue.heartbeat_worker("w1")
        self.queue.heartbeat_worker("w2")
        self.queue.forget_worker("w1")
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 1)

    def test_forgetting_an_unknown_worker_is_harmless(self) -> None:
        self.queue.forget_worker("never-existed")
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 0)

    def test_last_heartbeat_is_the_freshest_of_several(self) -> None:
        self.queue.heartbeat_worker("old")
        self.clock.now += 50
        self.queue.heartbeat_worker("new")
        presence = self.queue.worker_presence(stale_after=90)
        self.assertAlmostEqual(presence.seconds_since_heartbeat, 0.0, places=3)
        self.assertEqual(presence.online, 2)

    def test_presence_is_independent_of_queue_depth(self) -> None:
        """A busy queue with no worker is exactly the case the banner is for."""
        for name in ("a", "b"):
            self.queue.enqueue(name)
        self.assertEqual(self.queue.stats().waiting, 2)
        self.assertEqual(self.queue.worker_presence(stale_after=90).online, 0)

    def test_waiting_counts_ready_and_delayed(self) -> None:
        self.queue.enqueue("now")
        self.queue.enqueue("later", delay_seconds=30)
        stats = self.queue.stats()
        self.assertEqual((stats.ready, stats.delayed), (1, 1))
        self.assertEqual(stats.waiting, 2)


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
