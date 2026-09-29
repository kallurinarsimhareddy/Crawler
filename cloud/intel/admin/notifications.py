"""In-app notifications.

A notification with ``user_id`` goes to that member; ``user_id = None`` goes to
everyone in the workspace. Read state is per row (a broadcast marked read is
read for everyone — broadcasts are for workspace-wide facts, not personal to-dos).
Nothing here sends email or calls outside services; Slack/webhook fan-out is the
integrations service's job and only runs where an integration is configured.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from cloud.intel.core.context import Ctx, ForbiddenError, utcnow

__all__ = ["NotificationService"]

SEVERITIES = ("info", "success", "warning", "error")


class NotificationService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    def notify(self, ctx: Ctx, *, title: str, body: Optional[str] = None, kind: str = "system",
               user_id: Optional[str] = None, link: Optional[str] = None, severity: str = "info",
               entity_type: Optional[str] = None, entity_id: Optional[str] = None) -> Dict[str, Any]:
        system = ctx if ctx.system else ctx.as_system()
        return self.store.insert(system, "notifications", {
            "title": str(title)[:300], "body": (body or None) and str(body)[:2000], "kind": str(kind)[:60],
            "user_id": user_id, "link": (link or None) and str(link)[:500],
            "severity": severity if severity in SEVERITIES else "info",
            "entity_type": entity_type, "entity_id": entity_id})

    def _mine(self, ctx: Ctx, row: Dict[str, Any]) -> bool:
        return row.get("user_id") in (None, ctx.user_id)

    def list(self, ctx: Ctx, *, unread_only: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
        filters: Dict[str, Any] = {}
        if unread_only:
            filters["read_at__isnull"] = True
        rows = self.store.all(ctx, "notifications", filters, order="-created_at", cap=500)
        return [r for r in rows if self._mine(ctx, r)][:max(1, min(limit, 200))]

    def unread_count(self, ctx: Ctx) -> int:
        return len(self.list(ctx, unread_only=True, limit=200))

    def mark_read(self, ctx: Ctx, notification_id: str) -> Dict[str, Any]:
        row = self.store.get(ctx, "notifications", notification_id)
        if not self._mine(ctx, row):
            raise ForbiddenError("not your notification")
        if row.get("read_at"):
            return row
        return self.store.update(ctx.as_system() if not ctx.can_write else ctx, "notifications", notification_id,
                                 {"read_at": utcnow()})

    def mark_all_read(self, ctx: Ctx) -> int:
        count = 0
        for row in self.list(ctx, unread_only=True, limit=200):
            self.mark_read(ctx, row["id"])
            count += 1
        return count
