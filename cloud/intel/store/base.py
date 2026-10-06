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
searchable columns. ``any_of`` takes a list of filter dicts and matches rows
that satisfy at least one of them (each dict is ANDed inside; groups nest), so
callers can express ``a AND (b OR c)`` without leaving the spec's columns.
``all_of`` is the AND twin (two conditions on one column).
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
#: Reserved filter key for OR groups (``{"any_of": [{...}, {...}]}``) and its parsed operator.
ANY_OF = "any_of"
ALL_OF = "all_of"
MAX_FILTER_DEPTH = 4


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


def parse_filters(spec: EntitySpec, filters: Optional[Mapping[str, Any]], _depth: int = 0
                  ) -> List[Tuple[str, str, Any]]:
    """``{"status": "open", "score__gte": 50}`` -> ``[("status","eq","open"), ("score","gte",50)]``.

    ``{"any_of": [{...}, {...}]}`` -> ``[("any_of", "or", [[...], [...]])]``."""
    out: List[Tuple[str, str, Any]] = []
    for key, value in (filters or {}).items():
        if key == "q":
            continue
        if key in (ANY_OF, ALL_OF):
            if _depth >= MAX_FILTER_DEPTH:
                raise ValidationError("filter groups are nested too deeply")
            if not isinstance(value, (list, tuple)) or not value or not all(isinstance(g, Mapping) for g in value):
                raise ValidationError(f"{key} must be a non-empty list of filter objects")
            if len(value) > 50:
                raise ValidationError(f"{key} accepts at most 50 groups")
            out.append((key, "or" if key == ANY_OF else "and",
                        [parse_filters(spec, group, _depth + 1) for group in value]))
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


def _holds(value: Any, op: str, expected: Any) -> bool:
    return (op == "in" and value in expected) or (op == "eq" and value == expected) or (op == "notnull" and value is not None)


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
        """Insert many rows with the same checks as :meth:`insert`, in one transaction where
        the backend supports it (PostgreSQL: one permission check, pipelined statements).
        All-or-nothing: a failing row rolls back the whole call."""
        spec = get_spec(entity)
        self._check_write(ctx, spec, "insert")
        now = utcnow()
        built = [{"id": new_id(spec.prefix), "workspace_id": ctx.workspace_id, "created_at": now, "updated_at": now,
                  "created_by": ctx.user_id, "version": 1, **clean_values(spec, values, for_insert=True)}
                 for values in rows]
        return self._insert_many(ctx, spec, built) if built else []

    def update_many(self, ctx: Ctx, entity: str, changes: Sequence[Tuple[str, Mapping[str, Any]]]
                    ) -> List[Dict[str, Any]]:
        """Apply ``(row_id, changes)`` pairs with the same checks as :meth:`update`, in one
        transaction where the backend supports it. Returns the updated rows; ids that do
        not exist in this workspace are skipped (not an error), so a caller can compare
        counts. All-or-nothing on a database error."""
        spec = get_spec(entity)
        self._check_write(ctx, spec, "update")
        cleaned = [(row_id, clean_values(spec, values, for_insert=False)) for row_id, values in changes]
        cleaned = [(row_id, values) for row_id, values in cleaned if values]
        return self._update_many(ctx, spec, cleaned) if cleaned else []

    def group_stats(self, ctx: Ctx, entity: str, column: str, filters: Optional[Mapping[str, Any]],
                    metrics: Mapping[str, Any]) -> Dict[Any, Dict[str, int]]:
        """Per value of ``column``: ``count`` plus one conditional count per metric, in ONE scan.
        A metric is a condition ``(column, op, value)`` — op ``in`` (list), ``eq`` or ``notnull`` —
        or a list of conditions that must all hold."""
        spec = get_spec(entity)
        norm: Dict[str, List[Tuple[str, str, Any]]] = {}
        for name, raw in metrics.items():
            conds = [raw] if isinstance(raw, tuple) else list(raw)
            for col, op, _value in conds + [(column, "eq", None)]:
                if col not in spec.columns and col not in COMMON_COLUMNS:
                    raise ValidationError(f"{entity} has no field {col!r}")
                if op not in ("in", "eq", "notnull"):
                    raise ValidationError(f"unsupported metric operator {op!r}")
            norm[name] = [tuple(c) for c in conds]
        q = (filters or {}).get("q")
        return self._group_stats(ctx, spec, column, parse_filters(spec, filters), str(q) if q else None, norm)

    def _group_stats(self, ctx: Ctx, spec: EntitySpec, column: str, filters, q, metrics) -> Dict[Any, Dict[str, int]]:
        out: Dict[Any, Dict[str, int]] = {}
        for row in self._filtered_rows(ctx, spec, filters, q):
            slot = out.setdefault(row.get(column), {"count": 0, **{m: 0 for m in metrics}})
            slot["count"] += 1
            for name, conds in metrics.items():
                if all(_holds(row.get(col), op, value) for col, op, value in conds):
                    slot[name] += 1
        return out

    def group_tags(self, ctx: Ctx, entity: str, column: str, tags_column: str,
                   filters: Optional[Mapping[str, Any]] = None) -> Dict[Any, Dict[str, int]]:
        """Per value of ``column``: how many matching rows carry each tag of ``tags_column``."""
        spec = get_spec(entity)
        for col in (column, tags_column):
            if col not in spec.columns and col not in COMMON_COLUMNS:
                raise ValidationError(f"{entity} has no field {col!r}")
        if spec.columns[tags_column].kind != "tags":
            raise ValidationError(f"{tags_column} is not a tags field")
        q = (filters or {}).get("q")
        return self._group_tags(ctx, spec, column, tags_column, parse_filters(spec, filters), str(q) if q else None)

    def _group_tags(self, ctx: Ctx, spec: EntitySpec, column: str, tags_column: str, filters, q
                    ) -> Dict[Any, Dict[str, int]]:
        out: Dict[Any, Dict[str, int]] = {}
        for row in self._filtered_rows(ctx, spec, filters, q):
            slot = out.setdefault(row.get(column), {})
            for tag in set(row.get(tags_column) or []):
                slot[tag] = slot.get(tag, 0) + 1
        return out

    def walk(self, ctx: Ctx, entity: str, filters: Optional[Mapping[str, Any]] = None, *,
             cap: int = 100_000, columns: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Every matching row in id order, paged by id (cheap at any depth, unlike offsets).
        ``columns`` limits each row to those fields (plus ``id``)."""
        rows: List[Dict[str, Any]] = []
        last = ""
        while len(rows) < cap:
            page = dict(filters or {})
            if last:
                page = {"all_of": [page, {"id__gt": last}]} if page else {"id__gt": last}
            batch = self.rows(ctx, entity, page, order="id", limit=MAX_PAGE, columns=columns)
            rows.extend(batch)
            if len(batch) < MAX_PAGE:
                break
            last = batch[-1]["id"]
        return rows[:cap]

    def _filtered_rows(self, ctx: Ctx, spec: EntitySpec, filters, q):
        raise NotImplementedError

    def update_where(self, ctx: Ctx, entity: str, filters: Mapping[str, Any], changes: Mapping[str, Any]) -> int:
        """Apply ``changes`` to every row matching ``filters`` (same write checks, workspace scope
        and RLS as :meth:`update_many`); returns how many rows changed. Backends do it in one
        statement; the default pages through the rows. ``filters`` must not be empty."""
        spec = get_spec(entity)
        self._check_write(ctx, spec, "update")
        if not filters:
            raise ValidationError("update_where needs filters")
        clean = clean_values(spec, changes, for_insert=False)
        if not clean:
            return 0
        return self._update_where(ctx, spec, filters, clean)

    def _update_where(self, ctx: Ctx, spec: EntitySpec, filters: Mapping[str, Any], clean: Dict[str, Any]) -> int:
        total, last_id = 0, ""
        while True:
            page = dict(filters)
            if last_id:
                page["id__gt"] = last_id
            rows = self.rows(ctx, spec.name, page, order="id", limit=1000)
            if not rows:
                return total
            last_id = rows[-1]["id"]
            total += len(self._update_many(ctx, spec, [(r["id"], dict(clean)) for r in rows]))

    def delete_many(self, ctx: Ctx, entity: str, row_ids: Sequence[str]) -> int:
        """Delete rows by id with the same checks as :meth:`delete`; ids outside this workspace
        or already gone are skipped. Returns how many rows were deleted."""
        spec = get_spec(entity)
        self._check_write(ctx, spec, "delete")
        ids = [row_id for row_id in dict.fromkeys(row_ids) if row_id]
        return self._delete_many(ctx, spec, ids) if ids else 0

    def _delete_many(self, ctx: Ctx, spec: EntitySpec, row_ids: List[str]) -> int:
        return sum(1 for row_id in row_ids if self._delete(ctx, spec, row_id))

    # Backends override these two for speed; the defaults keep per-row semantics.
    def _insert_many(self, ctx: Ctx, spec: EntitySpec, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self._insert(ctx, spec, row) for row in rows]

    def _update_many(self, ctx: Ctx, spec: EntitySpec, changes: List[Tuple[str, Dict[str, Any]]]
                     ) -> List[Dict[str, Any]]:
        out = []
        for row_id, values in changes:
            row = self._update(ctx, spec, row_id, values, None)
            if row is not None:
                out.append(row)
        return out

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

    def rows(self, ctx: Ctx, entity: str, filters: Optional[Mapping[str, Any]] = None, *,
             order: Optional[str] = None, limit: int = 50,
             columns: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """One page of matching rows without a total count — for batch jobs that walk a large
        table with keyset paging (``id__gt``), where counting every page would dominate.
        ``columns`` returns only those fields (plus ``id``): much less to read and transfer
        when rows carry large JSON or arrays."""
        spec = get_spec(entity)
        limit = max(1, min(int(limit), MAX_PAGE))
        q = (filters or {}).get("q")
        parsed, text, ordering = parse_filters(spec, filters), str(q).strip() if q else None, parse_order(spec, order)
        if columns:
            cols = ["id"] + [c for c in dict.fromkeys(columns) if c != "id"]
            for col in cols:
                if col not in spec.columns and col not in COMMON_COLUMNS:
                    raise ValidationError(f"{entity} has no field {col!r}")
            return self._rows_columns(ctx, spec, parsed, text, ordering, limit, cols)
        return self._rows(ctx, spec, parsed, text, ordering, limit)

    def _rows_columns(self, ctx: Ctx, spec: EntitySpec, filters, q, order, limit,
                      columns: List[str]) -> List[Dict[str, Any]]:
        return [{c: r.get(c) for c in columns} for r in self._rows(ctx, spec, filters, q, order, limit)]

    def _rows(self, ctx: Ctx, spec: EntitySpec, filters, q, order, limit) -> List[Dict[str, Any]]:
        return self._list(ctx, spec, filters, q, order, limit, 0).rows

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

    def count_by_day(self, ctx: Ctx, entity: str, column: str, filters: Optional[Mapping[str, Any]] = None
                     ) -> Dict[str, int]:
        """Rows per UTC calendar day of the timestamp ``column`` ({"YYYY-MM-DD": n}) — counted by
        the store (one GROUP BY in SQL), so a dashboard never loads the rows themselves."""
        spec = get_spec(entity)
        col = spec.columns.get(column) or COMMON_COLUMNS.get(column)
        if col is None or col.kind != "ts":
            raise ValidationError(f"{entity}.{column} is not a timestamp field")
        return self._count_by_day(ctx, spec, column, parse_filters(spec, filters))

    def _count_by_day(self, ctx: Ctx, spec: EntitySpec, column: str, filters) -> Dict[str, int]:
        raise NotImplementedError

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

    def remove_member(self, ctx: Ctx, user_id: str) -> bool:  # pragma: no cover - implemented by stores
        raise NotImplementedError

    @abstractmethod
    def update_workspace(self, ctx: Ctx, **changes: Any) -> Dict[str, Any]: ...

    # --- plumbing ---------------------------------------------------------

    def _check_write(self, ctx: Ctx, spec: EntitySpec, op: str) -> None:
        ctx.require_write()
        if ctx.system:
            return
        if spec.admin_only:
            ctx.require_admin()
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


def _task_priority(task: Dict[str, Any], bulk_kinds: Sequence[str]) -> tuple:
    raw = (task.get("params") or {}).get("priority")
    try:
        priority = int(raw)
    except (TypeError, ValueError):
        priority = 100
    due = task.get("run_after") or task.get("created_at")
    return (1 if task.get("kind") in bulk_kinds else 0, priority, due, task.get("created_at"), task.get("id"))


def _generic_due_tasks(self, *, now, limit: int = 1, bulk_kinds: Sequence[str] = (),
                       ids: Optional[Sequence[str]] = None) -> List[Tuple[str, str]]:
    """System-only: ``(workspace_id, task_id)`` of runnable tasks across workspaces, in the
    order a worker should take them (interactive before bulk, priority, then due time)."""
    from datetime import timedelta

    found = []
    for workspace_id in self.list_workspace_ids():
        ctx = Ctx.for_system(workspace_id)
        filters: Dict[str, Any] = {"status": ["queued", "retrying"], "cancel_requested_at__isnull": True}
        if ids:
            filters["id"] = list(ids)
        for task in self.rows(ctx, "platform_tasks", filters, order="created_at", limit=MAX_PAGE):
            if task.get("run_after") is None or task["run_after"] <= now + timedelta(seconds=1):
                found.append((workspace_id, task))
    found.sort(key=lambda pair: _task_priority(pair[1], bulk_kinds))
    return [(w, t["id"]) for w, t in found[:max(0, limit)]]


def _generic_task_queue_counts(self, *, now) -> Dict[str, int]:
    counts = {"ready": 0, "delayed": 0, "running": 0}
    for workspace_id in self.list_workspace_ids():
        ctx = Ctx.for_system(workspace_id)
        for task in self.rows(ctx, "platform_tasks", {"status": ["queued", "retrying", "running"]}, limit=MAX_PAGE):
            if task["status"] == "running":
                counts["running"] += 1
            elif task.get("run_after") is not None and task["run_after"] > now:
                counts["delayed"] += 1
            else:
                counts["ready"] += 1
    return counts


#: System-only queue reads used by the PostgreSQL task queue (generic versions; the
#: PostgreSQL store replaces them with single indexed queries).
Store.due_tasks = _generic_due_tasks  # type: ignore[attr-defined]
Store.task_queue_counts = _generic_task_queue_counts  # type: ignore[attr-defined]
