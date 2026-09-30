"""An in-memory :class:`Store` for offline tests and the no-infrastructure dev mode.

It mirrors what PostgreSQL + RLS enforce: a user context sees only workspaces
it is a member of (by the *stored* membership, not the role the caller claims),
viewers cannot write, unique groups are enforced per workspace, and
``version`` increases on every update.
"""

from __future__ import annotations

import copy
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, NotFoundError, ValidationError, utcnow
from cloud.intel.store.base import Page, Store
from cloud.intel.store.spec import EntitySpec

__all__ = ["MemoryStore"]

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def _sort_key(value: Any) -> Tuple[int, Any]:
    if value is None:
        return (1, "")
    if isinstance(value, datetime):
        return (0, value.timestamp())
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    return (0, str(value).lower())


def _matches(row: Dict[str, Any], name: str, op: str, value: Any) -> bool:
    current = row.get(name)
    if op == "isnull":
        return (current is None) == bool(value)
    if op == "in":
        return current in value
    if op == "contains":
        return isinstance(current, list) and value in current
    if op == "ilike":
        return current is not None and str(value).lower() in str(current).lower()
    if op == "ne":
        return current != value
    if current is None:
        return False
    if op == "eq":
        return current == value
    if op == "gte":
        return current >= value
    if op == "lte":
        return current <= value
    if op == "gt":
        return current > value
    if op == "lt":
        return current < value
    raise ValidationError(f"unknown operator {op}")


class MemoryStore(Store):
    name = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._workspaces: Dict[str, Dict[str, Any]] = {}
        self._members: Dict[Tuple[str, str], str] = {}
        self._joined: Dict[Tuple[str, str], Any] = {}

    # --- workspaces --------------------------------------------------------

    def create_workspace(self, user_id: str, name: str, slug: str) -> Dict[str, Any]:
        user_id = str(uuid.UUID(user_id))
        if not _SLUG.match(slug or ""):
            raise ValidationError("slug must be 2-63 lowercase letters, digits or dashes")
        if not name or len(name) > 200:
            raise ValidationError("name must be 1-200 characters")
        with self._lock:
            if any(w["slug"] == slug for w in self._workspaces.values()):
                raise ConflictError(f"workspace slug {slug!r} is taken")
            ws = {
                "id": str(uuid.uuid4()), "name": name, "slug": slug, "created_by": user_id,
                "created_at": utcnow(), "updated_at": utcnow(), "ai_external_allowed": False, "settings": {},
            }
            self._workspaces[ws["id"]] = ws
            self._members[(ws["id"], user_id)] = "owner"
            self._joined[(ws["id"], user_id)] = ws["created_at"]
            return {**copy.deepcopy(ws), "role": "owner"}

    def workspaces_for(self, user_id: str) -> List[Dict[str, Any]]:
        user_id = str(uuid.UUID(user_id))
        with self._lock:
            out = [
                {**copy.deepcopy(self._workspaces[ws]), "role": role}
                for (ws, uid), role in self._members.items() if uid == user_id
            ]
        return sorted(out, key=lambda w: w["created_at"])

    def membership(self, user_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
        try:
            user_id, workspace_id = str(uuid.UUID(user_id)), str(uuid.UUID(workspace_id))
        except (ValueError, TypeError):
            return None
        with self._lock:
            role = self._members.get((workspace_id, user_id))
            if role is None:
                return None
            ws = self._workspaces[workspace_id]
            return {"workspace_id": workspace_id, "role": role, "name": ws["name"], "slug": ws["slug"],
                    "ai_external_allowed": ws["ai_external_allowed"], "settings": copy.deepcopy(ws["settings"])}

    def add_member(self, ctx: Ctx, user_id: str, role: str) -> None:
        self._authorize(ctx, admin=True)
        if role not in ("admin", "manager", "member", "viewer"):
            raise ValidationError("role must be admin, manager, member or viewer")
        key = (ctx.workspace_id, str(uuid.UUID(user_id)))
        with self._lock:
            self._members[key] = role
            self._joined.setdefault(key, utcnow())

    def remove_member(self, ctx: Ctx, user_id: str) -> bool:
        self._authorize(ctx, admin=True)
        key = (ctx.workspace_id, str(uuid.UUID(user_id)))
        with self._lock:
            if self._members.get(key) == "owner":
                raise ValidationError("the workspace owner cannot be removed")
            self._joined.pop(key, None)
            return self._members.pop(key, None) is not None

    def list_members(self, ctx: Ctx) -> List[Dict[str, Any]]:
        self._authorize(ctx)
        with self._lock:
            return [{"user_id": uid, "role": role, "created_at": self._joined.get((ws, uid))}
                    for (ws, uid), role in self._members.items() if ws == ctx.workspace_id]

    def update_workspace(self, ctx: Ctx, **changes: Any) -> Dict[str, Any]:
        self._authorize(ctx, admin=True)
        allowed = {"name", "settings", "ai_external_allowed"}
        bad = set(changes) - allowed
        if bad:
            raise ValidationError(f"cannot change {', '.join(sorted(bad))}")
        with self._lock:
            ws = self._workspaces[ctx.workspace_id]
            ws.update(changes)
            ws["updated_at"] = utcnow()
            return copy.deepcopy(ws)

    # --- authorization (what RLS does in PostgreSQL) -------------------------

    def _authorize(self, ctx: Ctx, *, write: bool = False, admin: bool = False) -> None:
        if ctx.system:
            if ctx.workspace_id not in self._workspaces:
                raise NotFoundError("workspace not found")
            return
        role = self._members.get((ctx.workspace_id, ctx.user_id))
        if role is None:
            raise NotFoundError("workspace not found")
        if admin and role not in ("owner", "admin"):
            raise ForbiddenError("workspace admin rights required")
        if write and role not in ("owner", "admin", "manager", "member"):
            raise ForbiddenError("this workspace role is read-only")

    def _table(self, spec: EntitySpec) -> Dict[str, Dict[str, Any]]:
        return self._tables.setdefault(spec.table, {})

    def _check_unique(self, spec: EntitySpec, row: Dict[str, Any], ignore_id: Optional[str] = None) -> None:
        for group in spec.unique:
            values = tuple(row.get(c) for c in group)
            if any(v is None for v in values):
                continue
            for other in self._table(spec).values():
                if other["id"] == ignore_id or other["workspace_id"] != row["workspace_id"]:
                    continue
                if tuple(other.get(c) for c in group) == values:
                    raise ConflictError(f"{spec.name} with the same {', '.join(group)} already exists")

    # --- Store primitives -----------------------------------------------------

    def _insert(self, ctx: Ctx, spec: EntitySpec, row: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._authorize(ctx, write=True)
            self._check_unique(spec, row)
            self._table(spec)[row["id"]] = copy.deepcopy(row)
            return copy.deepcopy(row)

    def _insert_many(self, ctx: Ctx, spec: EntitySpec, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """All-or-nothing, like the PostgreSQL transaction: a failing row restores the table."""
        with self._lock:
            table = self._table(spec)
            before = dict(table)
            try:
                return [self._insert(ctx, spec, row) for row in rows]
            except Exception:
                table.clear()
                table.update(before)
                raise

    def _update_many(self, ctx: Ctx, spec: EntitySpec, changes: List[Tuple[str, Dict[str, Any]]]
                     ) -> List[Dict[str, Any]]:
        with self._lock:
            table = self._table(spec)
            before = dict(table)  # _update replaces row dicts, so a shallow copy is a full snapshot
            try:
                return [row for row in (self._update(ctx, spec, row_id, values, None) for row_id, values in changes)
                        if row is not None]
            except Exception:
                table.clear()
                table.update(before)
                raise

    def _hidden(self, ctx: Ctx, spec: EntitySpec) -> bool:
        """An ``admin_only`` table is invisible to members below admin (RLS ``can_admin``)."""
        return bool(spec.admin_only) and not ctx.system and             self._members.get((ctx.workspace_id, ctx.user_id)) not in ("owner", "admin")

    def _visible(self, ctx: Ctx, spec: EntitySpec, row_id: str) -> Optional[Dict[str, Any]]:
        row = self._table(spec).get(row_id)
        if row is None or row["workspace_id"] != ctx.workspace_id or self._hidden(ctx, spec):
            return None
        return row

    def _update(self, ctx: Ctx, spec: EntitySpec, row_id: str, changes: Dict[str, Any],
                expected_version: Optional[int]) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._authorize(ctx, write=True)
            row = self._visible(ctx, spec, row_id)
            if row is None:
                return None
            if expected_version is not None and row["version"] != expected_version:
                return None
            candidate = {**row, **changes}
            self._check_unique(spec, candidate, ignore_id=row_id)
            candidate["version"] = row["version"] + 1
            candidate["updated_at"] = utcnow()
            self._table(spec)[row_id] = candidate
            return copy.deepcopy(candidate)

    def _delete(self, ctx: Ctx, spec: EntitySpec, row_id: str) -> bool:
        with self._lock:
            self._authorize(ctx, write=True)
            if self._visible(ctx, spec, row_id) is None:
                return False
            del self._table(spec)[row_id]
            return True

    def _get(self, ctx: Ctx, spec: EntitySpec, row_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                self._authorize(ctx)
            except NotFoundError:
                return None
            row = self._visible(ctx, spec, row_id)
            return copy.deepcopy(row) if row else None

    def _filtered(self, ctx: Ctx, spec: EntitySpec, filters, q) -> List[Dict[str, Any]]:
        try:
            self._authorize(ctx)
        except NotFoundError:
            return []
        if self._hidden(ctx, spec):
            return []
        rows = [r for r in self._table(spec).values() if r["workspace_id"] == ctx.workspace_id]
        for name, op, value in filters:
            rows = [r for r in rows if _matches(r, name, op, value)]
        if q:
            needle = q.lower()
            rows = [r for r in rows if any(r.get(c) and needle in str(r.get(c)).lower() for c in spec.searchable)]
        return rows

    def _list(self, ctx: Ctx, spec: EntitySpec, filters, q, order, limit, offset) -> Page:
        with self._lock:
            rows = self._filtered(ctx, spec, filters, q)
            for name, desc in reversed(order):
                present = [r for r in rows if r.get(name) is not None]
                missing = [r for r in rows if r.get(name) is None]
                present.sort(key=lambda r: _sort_key(r.get(name)), reverse=desc)
                rows = present + missing  # NULLS LAST either way, as the SQL does
            total = len(rows)
            return Page([copy.deepcopy(r) for r in rows[offset:offset + limit]], total, limit, offset)

    def _group_count(self, ctx: Ctx, spec: EntitySpec, column: str, filters, q) -> Dict[Any, int]:
        with self._lock:
            counts: Dict[Any, int] = {}
            for row in self._filtered(ctx, spec, filters, q):
                values = row.get(column)
                for value in (values if isinstance(values, list) else [values]):
                    counts[value] = counts.get(value, 0) + 1
            return counts


def _memory_list_workspace_ids(self: MemoryStore) -> List[str]:
    with self._lock:
        return list(self._workspaces)


MemoryStore.list_workspace_ids = _memory_list_workspace_ids  # type: ignore[attr-defined]


def _memory_system_membership(self: MemoryStore, workspace_id: str) -> Optional[Dict[str, Any]]:
    with self._lock:
        ws = self._workspaces.get(workspace_id)
        if ws is None:
            return None
        return {"workspace_id": ws["id"], "name": ws["name"], "slug": ws["slug"],
                "ai_external_allowed": ws["ai_external_allowed"], "settings": copy.deepcopy(ws["settings"])}


MemoryStore.system_membership = _memory_system_membership  # type: ignore[attr-defined]
