"""The store contract every platform service is written against.

Two implementations share it and one contract test suite
(``cloud/tests/test_platform_store_contract.py``):
:class:`~cloud.intel.store.memory.MemoryStore` and
:class:`~cloud.intel.store.postgres.PostgresStore`.

Rows are plain dicts. The store owns ``id``, ``workspace_id``, ``created_at``,
``updated_at``, ``created_by`` and ``version``; callers supply the rest.
Values are validated against the entity spec before anything is written, so the
two stores reject exactly the same input.

**Filters** are a dict of ``column -> value``. Plain values mean equality;
a list means "any of"; ``None`` means "is null". Suffixes add operators:
``col__gte``, ``col__lte``, ``col__gt``, ``col__lt``, ``col__ne``,
``col__contains`` (tags contain the value), ``col__ilike`` (substring,
case-insensitive), ``col__isnull`` (bool). ``q`` searches the spec's
searchable columns.
"""

from __future__ import annotations

import math
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cloud.intel.core.context import ConflictError, Ctx, NotFoundError, ValidationError, new_id, utcnow
from cloud.intel.store.spec import COMMON_COLUMNS, EntitySpec, get_spec

__all__ = ["Page", "Store", "clean_values", "parse_filters", "OPERATORS", "MAX_PAGE"]

MAX_PAGE = 500
OPERATORS = ("eq", "gte", "lte", "gt", "lt", "ne", "contains", "ilike", "isnull", "in")


@dataclass(frozen=True)
class Page:
    rows: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.rows) < self.total


def _coerce(spec: EntitySpec, name: str, value: Any) -> Any:
    col = spec.columns.get(name) or COMMON_COLUMNS.get(name)
    if col is None:
        raise ValidationError(f"{spec.name} has no field {name!r}")
    if value is None:
        if col.required and col.default is None and name not in COMMON_COLUMNS:
            raise ValidationError(f"{spec.name}.{name} is required")
        return None
    kind = col.kind
    try:
        if kind == "text":
            value = str(value).strip()
            if value == "" and not col.required:
                return None
            if col.max_len is not None and len(value) > col.max_len:
                raise ValidationError(f"{spec.name}.{name} is longer than {col.max_len} characters")
            if col.choices and value not in col.choices:
                raise ValidationError(f"{spec.name}.{name} must be one of {', '.join(col.choices)}")
        elif kind in ("int", "bigint"):
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, float) and not value.is_integer():
                raise ValueError
            value = int(value)
        elif kind == "float":
            if isinstance(value, bool):
                raise ValueError
            value = float(value)
            if math.isnan(value) or math.isinf(value):
                raise ValueError
        elif kind == "bool":
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "1", "yes", "y"):
                    value = True
                elif lowered in ("false", "0", "no", "n"):
                    value = False
                else:
                    raise ValueError
            value = bool(value)
        elif kind == "json":
            if not isinstance(value, (dict, list)):
                raise ValueError
        elif kind == "tags":
            if isinstance(value, str):
                value = [value]
            items = []
            for item in value:
                item = str(item).strip()
                if item and item not in items:
                    items.append(item[:200])
            value = items
        elif kind == "uuid":
            value = str(uuid.UUID(str(value)))
        elif kind == "ts":
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not isinstance(value, datetime):
                raise ValueError
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
        elif kind == "date":
            if isinstance(value, datetime):
                value = value.date()
            elif isinstance(value, str):
                value = date.fromisoformat(value[:10])
            if not isinstance(value, date):
                raise ValueError
    except ValidationError:
        raise
    except (TypeError, ValueError):
        raise ValidationError(f"{spec.name}.{name} is not a valid {kind}") from None
    if kind in ("int", "bigint", "float"):
        if col.minimum is not None and value < col.minimum:
            raise ValidationError(f"{spec.name}.{name} must be at least {col.minimum:g}")
        if col.maximum is not None and value > col.maximum:
            raise ValidationError(f"{spec.name}.{name} must be at most {col.maximum:g}")
    return value


def clean_values(spec: EntitySpec, values: Mapping[str, Any], *, for_insert: bool) -> Dict[str, Any]:
    """Validate caller-supplied values against the spec. Managed columns are refused."""
    out: Dict[str, Any] = {}
    for name, value in values.items():
        if name in COMMON_COLUMNS:
            raise ValidationError(f"{name} is managed by the platform")
        if name not in spec.columns:
            raise ValidationError(f"{spec.name} has no field {name!r}")
        if not for_insert and spec.columns[name].immutable:
            raise ValidationError(f"{spec.name}.{name} cannot be changed")
        out[name] = _coerce(spec, name, value)
        if not for_insert and out[name] is None and spec.columns[name].required:
            raise ValidationError(f"{spec.name}.{name} is required")
    if for_insert:
        for name, col in spec.columns.items():
            if name not in out or out[name] is None:
                if col.default is not None:
                    default = col.default
                    out[name] = list(default) if isinstance(default, list) else (
                        dict(default) if isinstance(default, dict) else default)
                elif col.required:
                    raise ValidationError(f"{spec.name}.{name} is required")
                else:
                    out.setdefault(name, None)
    return out


def parse_filters(spec: EntitySpec, filters: Optional[Mapping[str, Any]]) -> List[Tuple[str, str, Any]]:
    """``{"status": "open", "score__gte": 50}`` -> ``[("status","eq","open"), ("score","gte",50)]``."""
    out: List[Tuple[str, str, Any]] = []
    for key, value in (filters or {}).items():
        if key == "q":
            continue
        name, _, op = key.partition("__")
        op = op or ("in" if isinstance(value, (list, tuple, set)) else "eq")
        if op not in OPERATORS:
            raise ValidationError(f"unknown filter operator {op!r}")
        if name not in spec.columns and name not in COMMON_COLUMNS:
            raise ValidationError(f"{spec.name} has no field {name!r}")
        if op == "isnull":
            out.append((name, op, bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes")))
            continue
        if op == "in":
            values = [_coerce(spec, name, v) if v is not None else None for v in value]
            out.append((name, op, values))
            continue
        if op in ("contains", "ilike"):
            out.append((name, op, str(value)))
            continue
        if value is None:
            out.append((name, "isnull", True))
            continue
        col = spec.columns.get(name) or COMMON_COLUMNS[name]
        if col.kind == "tags":
            out.append((name, "contains", str(value)))
            continue
        out.append((name, op, _coerce(spec, name, value)))
    return out


def parse_order(spec: EntitySpec, order: Optional[str]) -> List[Tuple[str, bool]]:
    """``"-score,name"`` or ``"score desc"`` -> ``[("score", True), ("name", False)]`` (True = descending)."""
    order = order or spec.default_order
    result: List[Tuple[str, bool]] = []
    for part in order.split(","):
        part = part.strip()
        if not part:
            continue
        desc = False
        if part.startswith("-"):
            desc, part = True, part[1:]
        if " " in part:
            part, direction = part.split(None, 1)
            desc = direction.strip().lower() == "desc"
        if part not in spec.columns and part not in COMMON_COLUMNS:
            raise ValidationError(f"cannot sort {spec.name} by {part!r}")
        result.append((part, desc))
    if not any(name == "id" for name, _ in result):
        result.append(("id", result[0][1] if result else True))
    return result


class Store(ABC):
    """Workspace-scoped CRUD over the platform's entities."""

    name = "store"

    # --- write -----------------------------------------------------------

    def insert(self, ctx: Ctx, entity: str, values: Mapping[str, Any]) -> Dict[str, Any]:
        spec = get_spec(entity)
        self._check_write(ctx, spec, "insert")
        clean = clean_values(spec, values, for_insert=True)
        now = utcnow()
        row = {
            "id": new_id(spec.prefix),
            "workspace_id": ctx.workspace_id,
            "created_at": now,
            "updated_at": now,
            "created_by": ctx.user_id,
            "version": 1,
            **clean,
        }
        return self._insert(ctx, spec, row)

    def insert_many(self, ctx: Ctx, entity: str, rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        return [self.insert(ctx, entity, row) for row in rows]

    def update(self, ctx: Ctx, entity: str, row_id: str, changes: Mapping[str, Any], *,
               expected_version: Optional[int] = None) -> Dict[str, Any]:
        spec = get_spec(entity)
        self._check_write(ctx, spec, "update")
        clean = clean_values(spec, changes, for_insert=False)
        if not clean:
            return self.get(ctx, entity, row_id)
        row = self._update(ctx, spec, row_id, clean, expected_version)
        if row is None:
            if expected_version is not None and self._get(ctx, spec, row_id) is not None:
                raise ConflictError(f"{entity} {row_id} was changed by someone else; reload and retry")
            raise NotFoundError(f"{entity} {row_id} not found")
        return row

    def delete(self, ctx: Ctx, entity: str, row_id: str) -> None:
        spec = get_spec(entity)
        self._check_write(ctx, spec, "delete")
        if not self._delete(ctx, spec, row_id):
            raise NotFoundError(f"{entity} {row_id} not found")

    # --- read ------------------------------------------------------------

    def get(self, ctx: Ctx, entity: str, row_id: str) -> Dict[str, Any]:
        row = self.find(ctx, entity, row_id)
        if row is None:
            raise NotFoundError(f"{entity} {row_id} not found")
        return row

    def find(self, ctx: Ctx, entity: str, row_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(row_id, str) or len(row_id) > 60:
            return None
        return self._get(ctx, get_spec(entity), row_id)

    def list(self, ctx: Ctx, entity: str, filters: Optional[Mapping[str, Any]] = None, *,
             order: Optional[str] = None, limit: int = 50, offset: int = 0) -> Page:
        spec = get_spec(entity)
        limit = max(1, min(int(limit), MAX_PAGE))
        offset = max(0, int(offset))
        q = (filters or {}).get("q")
        return self._list(ctx, spec, parse_filters(spec, filters), str(q).strip() if q else None,
                          parse_order(spec, order), limit, offset)

    def all(self, ctx: Ctx, entity: str, filters: Optional[Mapping[str, Any]] = None, *,
            order: Optional[str] = None, cap: int = 100_000) -> List[Dict[str, Any]]:
        """Every matching row, paging internally. ``cap`` protects against runaway reads."""
        rows: List[Dict[str, Any]] = []
        offset = 0
        while len(rows) < cap:
            page = self.list(ctx, entity, filters, order=order, limit=MAX_PAGE, offset=offset)
            rows.extend(page.rows)
            if not page.has_more or not page.rows:
                break
            offset += len(page.rows)
        return rows[:cap]

    def first(self, ctx: Ctx, entity: str, filters: Mapping[str, Any], *, order: Optional[str] = None
              ) -> Optional[Dict[str, Any]]:
        page = self.list(ctx, entity, filters, order=order, limit=1)
        return page.rows[0] if page.rows else None

    def count(self, ctx: Ctx, entity: str, filters: Optional[Mapping[str, Any]] = None) -> int:
        return self.list(ctx, entity, filters, limit=1).total

    def group_count(self, ctx: Ctx, entity: str, column: str, filters: Optional[Mapping[str, Any]] = None
                    ) -> Dict[Any, int]:
        spec = get_spec(entity)
        if column not in spec.columns and column not in COMMON_COLUMNS:
            raise ValidationError(f"{entity} has no field {column!r}")
        q = (filters or {}).get("q")
        return self._group_count(ctx, spec, column, parse_filters(spec, filters), str(q) if q else None)

    # --- workspaces --------------------------------------------------------

    @abstractmethod
    def create_workspace(self, user_id: str, name: str, slug: str) -> Dict[str, Any]: ...

    @abstractmethod
    def workspaces_for(self, user_id: str) -> List[Dict[str, Any]]:
        """Workspaces the user belongs to, each with the user's ``role``."""

    @abstractmethod
    def membership(self, user_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
        """``{"workspace_id","role","ai_external_allowed","name","slug"}`` or None."""

    @abstractmethod
    def add_member(self, ctx: Ctx, user_id: str, role: str) -> None: ...

    @abstractmethod
    def list_members(self, ctx: Ctx) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def update_workspace(self, ctx: Ctx, **changes: Any) -> Dict[str, Any]: ...

    # --- plumbing ---------------------------------------------------------

    def _check_write(self, ctx: Ctx, spec: EntitySpec, op: str) -> None:
        ctx.require_write()
        if ctx.system:
            return
        if spec.system_write:
            raise ValidationError(f"{spec.name} is written by the platform only")
        if spec.append_only and op != "insert":
            raise ValidationError(f"{spec.name} is append-only")

    @abstractmethod
    def _insert(self, ctx: Ctx, spec: EntitySpec, row: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    def _update(self, ctx: Ctx, spec: EntitySpec, row_id: str, changes: Dict[str, Any],
                expected_version: Optional[int]) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def _delete(self, ctx: Ctx, spec: EntitySpec, row_id: str) -> bool: ...

    @abstractmethod
    def _get(self, ctx: Ctx, spec: EntitySpec, row_id: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def _list(self, ctx: Ctx, spec: EntitySpec, filters: List[Tuple[str, str, Any]], q: Optional[str],
              order: List[Tuple[str, bool]], limit: int, offset: int) -> Page: ...

    @abstractmethod
    def _group_count(self, ctx: Ctx, spec: EntitySpec, column: str, filters: List[Tuple[str, str, Any]],
                     q: Optional[str]) -> Dict[Any, int]: ...

    def close(self) -> None:
        pass


def _abstract_list_workspace_ids(self) -> List[str]:  # pragma: no cover - replaced below
    raise NotImplementedError


#: System-only: every workspace id, for maintenance loops (reaping, monitors).
Store.list_workspace_ids = _abstract_list_workspace_ids  # type: ignore[attr-defined]
