"""Platform background tasks: crawl, discovery, scraper, enrichment, validation,
research, analytics, workflow, import merge, monitoring, signals, export.

Same guarantees as CareerCloud's crawl jobs (``cloud/shared/service.py``),
generalised to any task kind and scoped to a workspace:

* **PostgreSQL (the store) is the source of truth; the queue is a doorbell.**
  The queue carries ``"<workspace_id>/<task_id>"``.
* **Claims are atomic** — an optimistic update on ``version`` from ``queued`` or
  ``retrying`` to ``running``. A duplicate delivery loses the race and does
  nothing.
* **Leases and heartbeats.** A running task holds ``lease_expires_at``; the
  worker's heartbeat extends it. Every worker write is fenced on
  ``worker_id`` + ``attempts``.
* **Bounded retries** with exponential backoff: ``retrying`` + ``run_after``.
* **Dead-worker recovery:** :meth:`TaskService.reap` requeues (or fails) running
  tasks whose lease expired.
* **Idempotency:** submitting with the same ``idempotency_key`` returns the
  existing task instead of creating a second one.
* **Pause/resume/cancel** from the user; a running task sees cancellation and
  pause through :meth:`TaskReporter.is_cancelled` / ``should_pause``.

Status machine::

    queued ─► running ─► completed | failed | cancelled
      ▲  │       │ └──► retrying ─► running …  (bounded by max_attempts)
      │  └► cancelled / paused     └──► paused (on request, between units)
      └── paused ◄─ resume
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, utcnow
from cloud.intel.store.base import Store
from cloud.intel.store.spec import TASK_KINDS

__all__ = ["TaskService", "TaskReporter", "TERMINAL", "backoff_seconds"]

log = logging.getLogger(__name__)

TERMINAL = frozenset({"completed", "failed", "cancelled"})
TRANSITIONS: Dict[str, frozenset] = {
    "queued": frozenset({"running", "cancelled", "paused"}),
    "retrying": frozenset({"running", "cancelled", "paused"}),
    "paused": frozenset({"queued", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled", "retrying", "paused", "queued"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def backoff_seconds(attempt: int, *, base: float = 30.0, cap: float = 900.0) -> float:
    return min(cap, base * (2 ** max(0, attempt - 1)))


def queue_key(workspace_id: str, task_id: str) -> str:
    return f"{workspace_id}/{task_id}"


def parse_queue_key(key: str) -> tuple:
    workspace_id, _, task_id = key.partition("/")
    if not workspace_id or not task_id:
        raise ValueError(f"malformed task key {key!r}")
    return workspace_id, task_id


class TaskService:
    def __init__(self, store: Store, *, queue: Any = None, clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.queue = queue
        self._clock = clock

    # --- user actions ---------------------------------------------------------

    def submit(self, ctx: Ctx, kind: str, params: Optional[Mapping[str, Any]] = None, *,
               idempotency_key: Optional[str] = None, max_attempts: int = 3, entity_type: Optional[str] = None,
               entity_id: Optional[str] = None, delay_seconds: float = 0.0) -> Dict[str, Any]:
        if kind not in TASK_KINDS:
            raise ValidationError(f"unknown task kind {kind!r}")
        ctx.require_write()
        if idempotency_key:
            existing = self.store.first(ctx.as_system(), "platform_tasks", {"idempotency_key": idempotency_key})
            if existing is not None:
                if existing["kind"] != kind:
                    raise ConflictError("idempotency key already used for a different task kind")
                return existing
        run_after = self._clock() + timedelta(seconds=delay_seconds) if delay_seconds else None
        # Tasks are system_write: users never write the row directly, so a task's
        # attempts, lease and worker fields cannot be forged from the API.
        task = self.store.insert(ctx.as_system(), "platform_tasks", {
            "kind": kind, "status": "queued", "params": dict(params or {}), "max_attempts": max_attempts,
            "idempotency_key": idempotency_key, "entity_type": entity_type, "entity_id": entity_id,
            "run_after": run_after, "progress": {"message": "Queued"},
        })
        audit(self.store, ctx, "task.submit", entity_type="platform_tasks", entity_id=task["id"],
              summary=f"{kind} task queued")
        self._enqueue(ctx.workspace_id, task["id"], delay_seconds)
        return task

    def get(self, ctx: Ctx, task_id: str) -> Dict[str, Any]:
        return self.store.get(ctx, "platform_tasks", task_id)

    def cancel(self, ctx: Ctx, task_id: str) -> Dict[str, Any]:
        ctx.require_write()
        task = self.get(ctx, task_id)
        if task["status"] in TERMINAL:
            raise ConflictError(f"task is already {task['status']}")
        system = ctx.as_system()
        if task["status"] in ("queued", "retrying", "paused"):
            updated = self._transition(system, task, "cancelled", finished_at=self._clock(),
                                       progress={**task["progress"], "message": "Cancelled"})
        else:
            updated = self.store.update(system, "platform_tasks", task_id,
                                        {"cancel_requested_at": self._clock()}, expected_version=task["version"])
        audit(self.store, ctx, "task.cancel", entity_type="platform_tasks", entity_id=task_id)
        return updated

    def pause(self, ctx: Ctx, task_id: str) -> Dict[str, Any]:
        ctx.require_write()
        task = self.get(ctx, task_id)
        if task["status"] in ("queued", "retrying"):
            return self._transition(ctx.as_system(), task, "paused", progress={**task["progress"], "message": "Paused"})
        if task["status"] == "running":
            progress = {**task["progress"], "pause_requested": True}
            return self.store.update(ctx.as_system(), "platform_tasks", task_id, {"progress": progress},
                                     expected_version=task["version"])
        raise ConflictError(f"cannot pause a {task['status']} task")

    def resume(self, ctx: Ctx, task_id: str) -> Dict[str, Any]:
        ctx.require_write()
        task = self.get(ctx, task_id)
        if task["status"] != "paused":
            raise ConflictError(f"cannot resume a {task['status']} task")
        progress = {k: v for k, v in task["progress"].items() if k != "pause_requested"}
        updated = self._transition(ctx.as_system(), task, "queued", progress={**progress, "message": "Resumed"})
        self._enqueue(ctx.workspace_id, task_id, 0)
        return updated

    # --- worker actions -------------------------------------------------------

    def claim(self, ctx: Ctx, task_id: str, *, worker_id: str, lease_seconds: float) -> Optional[Dict[str, Any]]:
        """Atomically take a task. ``None`` if it is not claimable (duplicate delivery,
        cancelled, paused, not yet due, attempts exhausted)."""
        task = self.store.find(ctx, "platform_tasks", task_id)
        if task is None or task["status"] not in ("queued", "retrying"):
            return None
        now = self._clock()
        if task["run_after"] is not None and task["run_after"] > now + timedelta(seconds=1):
            return None
        if task["cancel_requested_at"] is not None:
            return None
        if task["attempts"] >= task["max_attempts"]:
            self._safe_update(ctx, task, {"status": "failed", "finished_at": now,
                                          "error": task["error"] or "attempts exhausted"})
            return None
        try:
            return self.store.update(ctx, "platform_tasks", task_id, {
                "status": "running", "attempts": task["attempts"] + 1, "worker_id": worker_id,
                "heartbeat_at": now, "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "started_at": task["started_at"] or now, "run_after": None,
            }, expected_version=task["version"])
        except ConflictError:
            return None

    def _fenced(self, ctx: Ctx, claimed: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        current = self.store.find(ctx, "platform_tasks", claimed["id"])
        if (current is None or current["status"] != "running" or current["worker_id"] != claimed["worker_id"]
                or current["attempts"] != claimed["attempts"]):
            return None
        return current

    def heartbeat(self, ctx: Ctx, claimed: Mapping[str, Any], *, lease_seconds: float,
                  progress: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
        current = self._fenced(ctx, claimed)
        if current is None:
            return None
        now = self._clock()
        changes: Dict[str, Any] = {"heartbeat_at": now, "lease_expires_at": now + timedelta(seconds=lease_seconds)}
        if progress is not None:
            changes["progress"] = {**current["progress"], **progress}
        return self._safe_update(ctx, current, changes)

    def finish(self, ctx: Ctx, claimed: Mapping[str, Any], result: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        current = self._fenced(ctx, claimed)
        if current is None:
            return None
        return self._safe_update(ctx, current, {
            "status": "completed", "result": dict(result), "finished_at": self._clock(), "error": None,
            "lease_expires_at": None, "progress": {**current["progress"], "message": "Completed"}})

    def fail(self, ctx: Ctx, claimed: Mapping[str, Any], error: str, *, retryable: bool = True
             ) -> Optional[Dict[str, Any]]:
        current = self._fenced(ctx, claimed)
        if current is None:
            return None
        error = (error or "failed")[:4000]
        if retryable and current["attempts"] < current["max_attempts"]:
            delay = backoff_seconds(current["attempts"])
            updated = self._safe_update(ctx, current, {
                "status": "retrying", "error": error, "run_after": self._clock() + timedelta(seconds=delay),
                "lease_expires_at": None, "worker_id": None,
                "progress": {**current["progress"], "message": f"Retrying in {int(delay)}s: {error[:200]}"}})
            if updated is not None:
                self._enqueue(ctx.workspace_id, current["id"], delay)
            return updated
        return self._safe_update(ctx, current, {
            "status": "failed", "error": error, "finished_at": self._clock(), "lease_expires_at": None,
            "progress": {**current["progress"], "message": "Failed"}})

    def finish_cancelled(self, ctx: Ctx, claimed: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        current = self._fenced(ctx, claimed)
        if current is None:
            return None
        return self._safe_update(ctx, current, {"status": "cancelled", "finished_at": self._clock(),
                                                "lease_expires_at": None,
                                                "progress": {**current["progress"], "message": "Cancelled"}})

    def finish_paused(self, ctx: Ctx, claimed: Mapping[str, Any], checkpoint: Optional[Mapping[str, Any]] = None
                      ) -> Optional[Dict[str, Any]]:
        """The runner stopped at a safe point because pause was requested. The
        attempt is refunded; ``checkpoint`` lets the next run continue."""
        current = self._fenced(ctx, claimed)
        if current is None:
            return None
        progress = {k: v for k, v in current["progress"].items() if k != "pause_requested"}
        if checkpoint:
            progress["checkpoint"] = dict(checkpoint)
        return self._safe_update(ctx, current, {"status": "paused", "attempts": max(0, current["attempts"] - 1),
                                                "worker_id": None, "lease_expires_at": None,
                                                "progress": {**progress, "message": "Paused"}})

    def release(self, ctx: Ctx, claimed: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Worker shutting down: back to queued, attempt refunded."""
        current = self._fenced(ctx, claimed)
        if current is None:
            return None
        updated = self._safe_update(ctx, current, {"status": "queued", "attempts": max(0, current["attempts"] - 1),
                                                   "worker_id": None, "lease_expires_at": None})
        if updated is not None:
            self._enqueue(ctx.workspace_id, current["id"], 0)
        return updated

    def reap(self, ctx: Ctx, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Recover tasks whose worker died: lease expired while running."""
        now = self._clock()
        recovered = []
        stale = self.store.list(ctx, "platform_tasks", {"status": "running", "lease_expires_at__lt": now},
                                limit=limit).rows
        for task in stale:
            if task["cancel_requested_at"] is not None:
                changes = {"status": "cancelled", "finished_at": now, "lease_expires_at": None}
            elif task["attempts"] < task["max_attempts"]:
                delay = backoff_seconds(task["attempts"])
                changes = {"status": "retrying", "worker_id": None, "lease_expires_at": None,
                           "run_after": now + timedelta(seconds=delay),
                           "error": "worker stopped heartbeating; requeued"}
            else:
                changes = {"status": "failed", "finished_at": now, "lease_expires_at": None,
                           "error": "worker stopped heartbeating and attempts are exhausted"}
            updated = self._safe_update(ctx, task, changes)
            if updated is not None:
                recovered.append(updated)
                if updated["status"] == "retrying":
                    self._enqueue(ctx.workspace_id, task["id"], backoff_seconds(task["attempts"]))
        return recovered

    def requeue_orphans(self, ctx: Ctx, *, older_than_seconds: float = 600, limit: int = 100) -> int:
        """Queued tasks nobody picked up (lost doorbell): ring again. Idempotent."""
        cutoff = self._clock() - timedelta(seconds=older_than_seconds)
        count = 0
        for status in ("queued", "retrying"):
            for task in self.store.list(ctx, "platform_tasks", {"status": status, "updated_at__lt": cutoff},
                                        limit=limit).rows:
                due = task["run_after"]
                delay = max(0.0, (due - self._clock()).total_seconds()) if due else 0.0
                self._enqueue(ctx.workspace_id, task["id"], delay)
                count += 1
        return count

    # --- plumbing -------------------------------------------------------------

    def _transition(self, ctx: Ctx, task: Mapping[str, Any], status: str, **changes: Any) -> Dict[str, Any]:
        if status not in TRANSITIONS[task["status"]]:
            raise ConflictError(f"illegal task transition {task['status']} -> {status}")
        return self.store.update(ctx, "platform_tasks", task["id"], {"status": status, **changes},
                                 expected_version=task["version"])

    def _safe_update(self, ctx: Ctx, task: Mapping[str, Any], changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        status = changes.get("status")
        if status is not None and status != task["status"] and status not in TRANSITIONS[task["status"]]:
            log.warning("refusing task transition %s -> %s for %s", task["status"], status, task["id"])
            return None
        try:
            return self.store.update(ctx, "platform_tasks", task["id"], changes, expected_version=task["version"])
        except (ConflictError, NotFoundError):
            return None

    def _enqueue(self, workspace_id: str, task_id: str, delay: float) -> None:
        if self.queue is None:
            return
        try:
            self.queue.enqueue(queue_key(workspace_id, task_id), delay_seconds=delay)
        except Exception:  # noqa: BLE001 - the reaper re-rings lost doorbells
            log.exception("could not enqueue task %s; maintenance will retry", task_id)


@dataclass
class TaskReporter:
    """What a handler may ask of the system running it."""

    service: TaskService
    ctx: Ctx
    task: Dict[str, Any]
    lease_seconds: float = 120.0

    def progress(self, message: Optional[str] = None, **fields: Any) -> None:
        progress = dict(fields)
        if message:
            progress["message"] = message[:500]
        try:
            updated = self.service.heartbeat(self.ctx, self.task, lease_seconds=self.lease_seconds, progress=progress)
            if updated is not None:
                self.task = updated
        except Exception:  # noqa: BLE001 - progress must never break the work
            log.exception("progress update failed for %s", self.task.get("id"))

    def _current(self) -> Optional[Dict[str, Any]]:
        return self.service.store.find(self.ctx, "platform_tasks", self.task["id"])

    def is_cancelled(self) -> bool:
        current = self._current()
        return (current is None or current["cancel_requested_at"] is not None or current["status"] != "running"
                or current["worker_id"] != self.task["worker_id"] or current["attempts"] != self.task["attempts"])

    def should_pause(self) -> bool:
        current = self._current()
        return bool(current and current["progress"].get("pause_requested"))

    @property
    def checkpoint(self) -> Dict[str, Any]:
        return dict(self.task.get("progress", {}).get("checkpoint") or {})
