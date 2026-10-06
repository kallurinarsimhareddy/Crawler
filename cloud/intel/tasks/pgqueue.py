"""The platform task queue on PostgreSQL — no Redis.

``platform_tasks`` already is the queue: a task is runnable when it is ``queued`` (or
``retrying``) and due (``run_after`` empty or past). Claims stay in
:meth:`TaskService.claim` — an optimistic update on ``version``, so two workers can never
both take a task — and leases, heartbeats, retries with backoff, pause, cancel,
checkpoints and dead-worker recovery (:meth:`TaskService.reap`) are unchanged.

This class only answers "which task is due next?" and provides the doorbell:

* :meth:`reserve` reads the next due task across workspaces in one short, indexed query
  (``FOR UPDATE SKIP LOCKED`` skips a row another worker is claiming right now). Interactive
  kinds go before long bulk runs; ``params.priority`` (lower first) overrides.
* :meth:`enqueue` sends ``NOTIFY``; an idle worker :meth:`wait_for_work` on ``LISTEN``
  (one dedicated connection), so new work starts at once without polling. Delayed work
  (retries, scheduled runs) is noticed by the worker's periodic reserve
  (:attr:`housekeeping_seconds`).
* Worker presence comes from the worker's own state file (written every 60 s), so no
  table or queue command is needed for it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from cloud.shared.queue import Delivery, JobQueue, QueueStats, WorkerPresence

__all__ = ["PostgresTaskQueue", "CHANNEL", "BULK_KINDS"]

log = logging.getLogger(__name__)

#: LISTEN / NOTIFY channel for "a task is ready now".
CHANNEL = "careercloud_platform_tasks"
#: Long bulk runs: they wait behind interactive work (account builds, contact search,
#: email validation, exports...). ``params.priority`` overrides either way.
BULK_KINDS = ("job_monitor", "job_lifecycle", "crawl", "scraper", "discovery", "monitor")
#: A task the worker could not claim is not offered again for this long (another worker
#: holds it, or it is not claimable yet) — protects against a tight retry loop.
_COOLDOWN_SECONDS = 5.0


class PostgresTaskQueue(JobQueue):
    name = "postgres"
    #: The database is the source of truth: there are no lost doorbells to re-ring.
    durable = True
    #: How often an idle worker looks for delayed work that became due (one small query).
    housekeeping_seconds = 60.0

    def __init__(self, store: Any, *, url: Optional[str] = None, state_file: Optional[str] = None,
                 clock=None, only_ids: Optional[Sequence[str]] = None) -> None:
        self.store = store
        self._url = url
        self._state_file = state_file or os.environ.get("CAREERCLOUD_WORKER_STATE_FILE") or None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._only = list(only_ids) if only_ids else None
        self._recent: Dict[str, float] = {}
        self._listener: Any = None
        self._listener_lock = threading.Lock()
        # in-process doorbell (the memory store, or API and worker in one process)
        self._bell = threading.Condition()
        self._rang = False

    # ------------------------------------------------------------------ doorbell

    def enqueue(self, job_id: str, *, delay_seconds: float = 0.0) -> bool:
        """The task row (with its ``run_after``) is already stored; ring for immediate work."""
        if delay_seconds and delay_seconds > 0:
            return True
        with self._bell:
            self._rang = True
            self._bell.notify_all()
        notify = getattr(self.store, "notify", None)
        if callable(notify):
            notify(CHANNEL, job_id[:200])
        return True

    def wait_for_work(self, timeout: float) -> bool:
        with self._bell:
            if self._rang:
                self._rang = False
                return True
        if not self._url:
            with self._bell:
                if not self._rang:
                    self._bell.wait(max(0.0, timeout))
                rang, self._rang = self._rang, False
                return rang
        listener = self._listen()
        try:
            for _ in listener.notifies(timeout=max(0.1, float(timeout)), stop_after=1):
                return True
            return False
        except Exception:
            self._drop_listener()
            raise

    def interrupt(self) -> None:
        """Wake a blocked :meth:`wait_for_work` (worker shutdown)."""
        with self._bell:
            self._rang = True
            self._bell.notify_all()
        if self._listener is not None:
            notify = getattr(self.store, "notify", None)
            if callable(notify):
                notify(CHANNEL, "interrupt")

    def _listen(self) -> Any:
        with self._listener_lock:
            if self._listener is None or self._listener.closed:
                import psycopg

                conn = psycopg.connect(self._url, autocommit=True, connect_timeout=10,
                                       application_name="sana-worker-listen")
                conn.execute(f"LISTEN {CHANNEL}")
                self._listener = conn
            return self._listener

    def _drop_listener(self) -> None:
        with self._listener_lock:
            if self._listener is not None:
                try:
                    self._listener.close()
                except Exception:  # noqa: BLE001
                    pass
                self._listener = None

    # ------------------------------------------------------------------ reserve

    def reserve(self, worker_id: str, *, visibility_timeout: float) -> Optional[Delivery]:
        now_mono = time.monotonic()
        self._recent = {k: t for k, t in self._recent.items() if now_mono - t < _COOLDOWN_SECONDS}
        rows = self.store.due_tasks(now=self._clock(), limit=len(self._recent) + 1, bulk_kinds=BULK_KINDS,
                                    ids=self._only)
        for workspace_id, task_id in rows:
            key = f"{workspace_id}/{task_id}"
            if key in self._recent:
                continue
            self._recent[key] = now_mono
            return Delivery(key, worker_id, 1)
        return None

    def extend(self, delivery: Delivery, *, visibility_timeout: float) -> bool:
        return True                     # the lease lives in platform_tasks (TaskService.heartbeat)

    def ack(self, delivery: Delivery) -> None:
        """Nothing to acknowledge: the claim (or its absence) is recorded on the task row. The
        cooldown entry stays, so a task this worker could not claim is not re-offered at once."""

    # ------------------------------------------------------------------ status

    def stats(self) -> QueueStats:
        counts = self.store.task_queue_counts(now=self._clock())
        return QueueStats(int(counts.get("ready", 0)), int(counts.get("delayed", 0)), int(counts.get("running", 0)))

    def heartbeat_worker(self, worker_id: str) -> None:
        """Presence is the worker's state file (written on every presence beat)."""

    def forget_worker(self, worker_id: str) -> None:
        pass

    def worker_presence(self, *, stale_after: float = 180.0) -> WorkerPresence:
        now = time.time()
        state = self.worker_state()
        last = None
        try:
            last = datetime.fromisoformat(state["updated_at"]).timestamp() if state else None
        except (KeyError, TypeError, ValueError):
            last = None
        online = 1 if last is not None and now - last <= stale_after else 0
        return WorkerPresence(online=online, last_heartbeat=last, checked_at=now)

    def worker_state(self) -> Optional[Dict[str, Any]]:
        if not self._state_file:
            return None
        try:
            with open(self._state_file, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def ping(self) -> None:
        self.store.list_workspace_ids()

    def close(self) -> None:
        self._drop_listener()

    def due_keys(self, limit: int = 20) -> List[str]:
        """For operators and tests: the next due task keys, in reservation order."""
        return [f"{w}/{t}" for w, t in self.store.due_tasks(now=self._clock(), limit=limit,
                                                             bulk_kinds=BULK_KINDS, ids=self._only)]
