"""Who is acting, in which workspace, with what rights.

Every platform service call takes a :class:`Ctx`. A user context carries the
caller's verified user id and role in the workspace; the store turns it into a
row-level-security scoped transaction. A system context (worker, workflow
engine) is still bound to one workspace — nothing in the platform ever runs
"across all workspaces" by accident — but writes as the table owner.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional

__all__ = [
    "Ctx",
    "ConflictError",
    "ForbiddenError",
    "NotFoundError",
    "PlatformError",
    "ValidationError",
    "new_id",
    "utcnow",
    "READ_ROLES",
    "WRITE_ROLES",
    "ADMIN_ROLES",
    "MANAGER_ROLES",
]

READ_ROLES = frozenset({"owner", "admin", "manager", "member", "viewer"})
WRITE_ROLES = frozenset({"owner", "admin", "manager", "member"})
#: Managers write like members and also manage teams, ownership and assignment.
MANAGER_ROLES = frozenset({"owner", "admin", "manager"})
ADMIN_ROLES = frozenset({"owner", "admin"})


class PlatformError(Exception):
    """Base class; carries an HTTP-ish status for the API layer."""

    status = 400


class NotFoundError(PlatformError, LookupError):
    status = 404


class ForbiddenError(PlatformError, PermissionError):
    status = 403


class ConflictError(PlatformError):
    status = 409


class ValidationError(PlatformError, ValueError):
    status = 422


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


@dataclass(frozen=True)
class Ctx:
    workspace_id: str
    user_id: Optional[str] = None
    role: str = "viewer"
    #: System scope: worker/automation. Bypasses RLS (connects as table owner)
    #: but is still confined to ``workspace_id`` by every query.
    system: bool = False
    actor_kind: str = "user"
    request_id: Optional[str] = None
    #: Whether private workspace data may be sent to external AI providers.
    ai_external_allowed: bool = False
    #: Display label for the actor (the signed-in email), recorded on audit rows.
    actor_label: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", str(uuid.UUID(str(self.workspace_id))))
        if self.user_id is not None:
            object.__setattr__(self, "user_id", str(uuid.UUID(str(self.user_id))))
        if not self.system and self.user_id is None:
            raise ValueError("a user context needs a user id")
        if self.role not in READ_ROLES:
            raise ValueError(f"unknown role {self.role!r}")

    @classmethod
    def for_system(cls, workspace_id: str, *, actor_kind: str = "system", user_id: Optional[str] = None,
                   ai_external_allowed: bool = False) -> "Ctx":
        return cls(workspace_id=workspace_id, user_id=user_id, role="owner", system=True,
                   actor_kind=actor_kind, ai_external_allowed=ai_external_allowed)

    def as_system(self, actor_kind: str = "system") -> "Ctx":
        """The same workspace, acting as the system (e.g. a worker finishing a user's task)."""
        return replace(self, system=True, actor_kind=actor_kind, role="owner")

    @property
    def can_write(self) -> bool:
        return self.system or self.role in WRITE_ROLES

    @property
    def can_manage(self) -> bool:
        return self.system or self.role in MANAGER_ROLES

    def require_manager(self) -> None:
        if not self.can_manage:
            raise ForbiddenError("workspace manager rights required")

    @property
    def can_admin(self) -> bool:
        return self.system or self.role in ADMIN_ROLES

    def require_write(self) -> None:
        if not self.can_write:
            raise ForbiddenError("this workspace role is read-only")

    def require_admin(self) -> None:
        if not self.can_admin:
            raise ForbiddenError("workspace admin rights required")
