"""The job queue: how a job id travels from the API to a worker.

**The queue is a doorbell, not the source of truth.** PostgreSQL decides
whether a job may run (an atomic claim), who holds it (a lease), and whether it
is finished. The queue only says "job X may be ready". That makes delivery
problems harmless:

* delivered twice → the second claim fails, the delivery is acknowledged, done;
* lost (Redis flushed, API crashed between insert and enqueue) → the worker's
  reaper finds queued jobs nobody touched and enqueues them again;
* worker died mid-job → the delivery's visibility timeout expires and it is
  redelivered, *and* the job's lease expires and the reaper requeues it. The
  claim makes sure only one of those runs it.

**Semantics every implementation provides** (and ``test_queue.py`` checks for
both):

``enqueue(job_id, delay_seconds)``
    Idempotent while the id is waiting: enqueueing a job that is already
    ready or delayed does nothing and returns ``False``.
``reserve(worker_id, visibility_timeout)``
    Takes the oldest ready id (after promoting due delayed ones) and hides it
    for ``visibility_timeout`` seconds. If not acknowledged in time it becomes
    ready again.
``extend(delivery, visibility_timeout)``
    Pushes the deadline out; the worker calls it with every heartbeat.
``ack(delivery)``
    Forgets the delivery.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Set, Tuple

__all__ = ["Delivery", "InMemoryJobQueue", "JobQueue", "QueueStats", "RedisJobQueue"]


@dataclass(frozen=True)
class Delivery:
    job_id: str
    worker_id: str
    #: How many times this id has been handed out since it was last acknowledged.
    delivery_count: int


@dataclass(frozen=True)
class QueueStats:
    ready: int
    delayed: int
    in_flight: int


class JobQueue(ABC):
    """Hands job ids to workers. See the module docstring for the contract."""

    name: str = "queue"

    @abstractmethod
    def enqueue(self, job_id: str, *, delay_seconds: float = 0.0) -> bool:
        """Make ``job_id`` available now or after a delay. ``False`` if already waiting."""

    @abstractmethod
    def reserve(self, worker_id: str, *, visibility_timeout: float) -> Optional[Delivery]:
        """Take the next ready id, or ``None`` if there is none."""

    @abstractmethod
    def extend(self, delivery: Delivery, *, visibility_timeout: float) -> bool:
        """Keep an in-flight delivery hidden longer. ``False`` if it is no longer in flight."""

    @abstractmethod
    def ack(self, delivery: Delivery) -> None:
        """Finish with a delivery."""

    @abstractmethod
    def stats(self) -> QueueStats:
        """Counts, for health checks and tests."""

    def ping(self) -> None:
        """Raise if the queue backend is unreachable."""

    def close(self) -> None:
        """Release connections."""


class InMemoryJobQueue(JobQueue):
    """A queue in this process. For tests and single-process local development."""

    name = "memory"

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._ready: Deque[str] = deque()
        self._delayed: Dict[str, float] = {}
        self._waiting: Set[str] = set()
        self._in_flight: Dict[str, Tuple[float, str]] = {}
        self._deliveries: Dict[str, int] = {}

    def enqueue(self, job_id: str, *, delay_seconds: float = 0.0) -> bool:
        with self._lock:
            if job_id in self._waiting:
                return False
            self._waiting.add(job_id)
            if delay_seconds > 0:
                self._delayed[job_id] = self._clock() + delay_seconds
            else:
                self._ready.append(job_id)
            return True

    def _promote(self, now: float) -> None:
        for job_id, due in sorted(self._delayed.items(), key=lambda item: item[1]):
            if due <= now:
                del self._delayed[job_id]
                self._ready.append(job_id)
        for job_id, (deadline, _) in list(self._in_flight.items()):
            if deadline <= now:
                del self._in_flight[job_id]
                if job_id not in self._waiting:
                    self._waiting.add(job_id)
                    self._ready.append(job_id)

    def reserve(self, worker_id: str, *, visibility_timeout: float) -> Optional[Delivery]:
        with self._lock:
            now = self._clock()
            self._promote(now)
            if not self._ready:
                return None
            job_id = self._ready.popleft()
            self._waiting.discard(job_id)
            self._in_flight[job_id] = (now + visibility_timeout, worker_id)
            count = self._deliveries.get(job_id, 0) + 1
            self._deliveries[job_id] = count
            return Delivery(job_id, worker_id, count)

    def extend(self, delivery: Delivery, *, visibility_timeout: float) -> bool:
        with self._lock:
            if delivery.job_id not in self._in_flight:
                return False
            self._in_flight[delivery.job_id] = (self._clock() + visibility_timeout, delivery.worker_id)
            return True

    def ack(self, delivery: Delivery) -> None:
        with self._lock:
            self._in_flight.pop(delivery.job_id, None)
            if delivery.job_id not in self._waiting:
                self._deliveries.pop(delivery.job_id, None)

    def stats(self) -> QueueStats:
        with self._lock:
            return QueueStats(len(self._ready), len(self._delayed), len(self._in_flight))


# --- Redis ---------------------------------------------------------------------

# Keys, all under one prefix so an environment can share a Redis without collision:
#   {p}:ready       LIST   ids ready now; LPUSH to add, RPOP to take (FIFO)
#   {p}:delayed     ZSET   ids waiting for a retry delay; score = due time
#   {p}:waiting     SET    ids in ready or delayed, for idempotent enqueue
#   {p}:inflight    ZSET   reserved ids; score = visibility deadline
#   {p}:deliveries  HASH   id -> times delivered since last ack
#
# Every multi-key step is a Lua script, so it is atomic across workers. Times are
# passed in from the client rather than read with TIME, which keeps the scripts
# deterministic and testable; worker clocks need only agree to within seconds.

_ENQUEUE = """
if redis.call('SISMEMBER', KEYS[3], ARGV[1]) == 1 then return 0 end
redis.call('SADD', KEYS[3], ARGV[1])
if tonumber(ARGV[2]) > 0 then
  redis.call('ZADD', KEYS[2], tonumber(ARGV[3]) + tonumber(ARGV[2]), ARGV[1])
else
  redis.call('LPUSH', KEYS[1], ARGV[1])
end
return 1
"""

_RESERVE = """
local now = tonumber(ARGV[1])
local due = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
for _, id in ipairs(due) do
  redis.call('ZREM', KEYS[2], id)
  redis.call('LPUSH', KEYS[1], id)
end
local expired = redis.call('ZRANGEBYSCORE', KEYS[4], '-inf', now)
for _, id in ipairs(expired) do
  redis.call('ZREM', KEYS[4], id)
  if redis.call('SADD', KEYS[3], id) == 1 then
    redis.call('LPUSH', KEYS[1], id)
  end
end
local id = redis.call('RPOP', KEYS[1])
if not id then return nil end
redis.call('SREM', KEYS[3], id)
redis.call('ZADD', KEYS[4], now + tonumber(ARGV[2]), id)
local count = redis.call('HINCRBY', KEYS[5], id, 1)
return {id, count}
"""

_EXTEND = """
if redis.call('ZSCORE', KEYS[1], ARGV[1]) == false then return 0 end
redis.call('ZADD', KEYS[1], 'XX', tonumber(ARGV[2]), ARGV[1])
return 1
"""

_ACK = """
redis.call('ZREM', KEYS[1], ARGV[1])
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 0 then
  redis.call('HDEL', KEYS[3], ARGV[1])
end
return 1
"""


class RedisJobQueue(JobQueue):
    """The production queue.

    Args:
        client: A ``redis.Redis`` (or compatible, e.g. ``fakeredis`` in tests)
            created with ``decode_responses=True``.
        prefix: Key prefix; include the environment, e.g. ``careercloud:staging``.
        clock: Seconds since the epoch. Injected for tests.
    """

    name = "redis"

    def __init__(
        self,
        client: object,
        *,
        prefix: str = "careercloud",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not prefix or any(ch.isspace() for ch in prefix):
            raise ValueError("queue prefix must be a non-empty string without spaces")
        self._redis = client
        self._clock = clock
        self._keys = {
            name: f"{prefix}:{name}"
            for name in ("ready", "delayed", "waiting", "inflight", "deliveries")
        }
        register = client.register_script  # type: ignore[attr-defined]
        self._enqueue = register(_ENQUEUE)
        self._reserve = register(_RESERVE)
        self._extend = register(_EXTEND)
        self._ack = register(_ACK)

    @classmethod
    def from_url(cls, url: str, *, prefix: str) -> "RedisJobQueue":
        import redis  # imported here so the API can run without redis installed

        client = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=10, socket_connect_timeout=5, health_check_interval=30
        )
        return cls(client, prefix=prefix)

    def enqueue(self, job_id: str, *, delay_seconds: float = 0.0) -> bool:
        k = self._keys
        added = self._enqueue(
            keys=[k["ready"], k["delayed"], k["waiting"]],
            args=[job_id, max(0.0, float(delay_seconds)), self._clock()],
        )
        return bool(int(added))

    def reserve(self, worker_id: str, *, visibility_timeout: float) -> Optional[Delivery]:
        k = self._keys
        reply = self._reserve(
            keys=[k["ready"], k["delayed"], k["waiting"], k["inflight"], k["deliveries"]],
            args=[self._clock(), float(visibility_timeout)],
        )
        if not reply:
            return None
        job_id, count = reply
        return Delivery(str(job_id), worker_id, int(count))

    def extend(self, delivery: Delivery, *, visibility_timeout: float) -> bool:
        return bool(
            int(
                self._extend(
                    keys=[self._keys["inflight"]],
                    args=[delivery.job_id, self._clock() + float(visibility_timeout)],
                )
            )
        )

    def ack(self, delivery: Delivery) -> None:
        k = self._keys
        self._ack(keys=[k["inflight"], k["waiting"], k["deliveries"]], args=[delivery.job_id])

    def stats(self) -> QueueStats:
        k = self._keys
        pipe = self._redis.pipeline()  # type: ignore[attr-defined]
        pipe.llen(k["ready"])
        pipe.zcard(k["delayed"])
        pipe.zcard(k["inflight"])
        ready, delayed, in_flight = pipe.execute()
        return QueueStats(int(ready), int(delayed), int(in_flight))

    def ping(self) -> None:
        self._redis.ping()  # type: ignore[attr-defined]

    def close(self) -> None:
        close = getattr(self._redis, "close", None)
        if callable(close):
            close()

    def keys(self) -> List[str]:
        return list(self._keys.values())
