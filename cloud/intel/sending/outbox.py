"""The outbound queue: what was rendered for sending, and the periodic dispatcher.

Messages enter ``outbound_messages`` from the sequence engine (one row per
enrollment step; a retried step reuses its row). :meth:`OutboxService.tick`
runs on every worker maintenance pass: when email sending is not allowed in
this environment it returns immediately without touching the database, so it
costs nothing on development and staging. When sending *is* allowed it
advances due, approved enrollments through :meth:`SequenceService.process_due`
— which re-checks every gate (campaign sending, approval, suppression,
schedule window, caps, mailbox limits) before each message.

``run_send_task`` is the ``email_send`` background task: the same processing
for one workspace on demand (e.g. "process now" from the UI).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, ValidationError

__all__ = ["OutboxService", "run_send_task"]

log = logging.getLogger(__name__)


class OutboxService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    def sending_allowed(self) -> bool:
        return bool(self.platform.config.allow_email_sending)

    def tick(self, ctx: Ctx) -> int:
        """Advance due sequence steps; a no-op (no queries) unless sending is allowed."""
        if not self.sending_allowed():
            return 0
        stats = self.platform.service("sequences").process_due(ctx)
        return int(stats.get("processed", 0))

    def status(self, ctx: Ctx) -> Dict[str, Any]:
        by_status = {str(k): v for k, v in self.store.group_count(ctx, "outbound_messages", "status").items()}
        return {"sending_allowed": self.sending_allowed(),
                "environment": self.platform.config.environment,
                "why_not": None if self.sending_allowed() else
                "email sending is disabled in this environment (needs production and CAREERCLOUD_ALLOW_EMAIL_SENDING)",
                "by_status": by_status}

    def cancel(self, ctx: Ctx, message_id: str) -> Dict[str, Any]:
        ctx.require_write()
        row = self.store.get(ctx, "outbound_messages", message_id)
        if row["status"] not in ("queued", "scheduled", "blocked", "failed"):
            raise ValidationError(f"a {row['status']} message cannot be cancelled")
        row = self.store.update(ctx, "outbound_messages", message_id, {"status": "cancelled",
                                                                       "block_reason": "cancelled by a user"})
        audit(self.store, ctx, "outbox.cancel", entity_type="outbound_messages", entity_id=message_id)
        return row

    def process_now(self, ctx: Ctx) -> Dict[str, Any]:
        """Queue an ``email_send`` task for this workspace (admin only)."""
        ctx.require_admin()
        task = self.platform.tasks.submit(ctx, "email_send", {"reason": "manual"})
        return {"task": task, "sending_allowed": self.sending_allowed()}


def run_send_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    outbox: OutboxService = platform.service("outbox")
    if not outbox.sending_allowed():
        return {"processed": 0, "note": "email sending is disabled in this environment; nothing was sent"}
    stats = platform.service("sequences").process_due(ctx)
    reporter.progress(f"processed {stats.get('processed', 0)} due step(s)", done=1, total=1)
    audit(platform.store, ctx, "outbox.process", summary=f"{stats.get('sent', 0)} sent", changes=stats)
    return stats
