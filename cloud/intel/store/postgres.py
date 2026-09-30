"""The PostgreSQL / Supabase :class:`Store`.

A user context runs each call in a transaction that sets the request's JWT
claims and ``SET LOCAL ROLE authenticated`` — the same mechanism as
:class:`cloud.db.postgres.PostgresJobRepository` — so the RLS policies generated
in ``0003_platform.sql`` apply to every statement. Every query *also* filters on
``workspace_id``; RLS is the second lock, not the only one.

A system context (worker) runs as the connecting role, which owns the tables,
and is confined to its workspace by the explicit filter.

Identifiers come only from :mod:`cloud.intel.store.spec` and are quoted with
``psycopg.sql.Identifier``; values are always bound parameters.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, NotFoundError, ValidationError
from cloud.intel.store.base import Page, Store
from cloud.intel.store.spec import COMMON_COLUMNS, EntitySpec

__all__ = ["PostgresStore"]


def _plain(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()}


class PostgresStore(Store):
    name = "postgres"

    def __init__(self, pool: Any, *, user_role: str = "authenticated", owns_pool: bool = False) -> None:
        self._pool = pool
        self._user_role = user_role
        self._owns_pool = owns_pool

    @classmethod
    def from_url(cls, url: str, *, min_size: int = 1, max_size: int = 10,
                 user_role: str = "authenticated") -> "PostgresStore":
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(url, min_size=min_size, max_size=max_size,
                              kwargs={"row_factory": dict_row, "autocommit": False}, open=True,
                              name="careercloud-platform")
        return cls(pool, user_role=user_role, owns_pool=True)

    def close(self) -> None:
        if self._owns_pool:
            self._pool.close()

    # --- plumbing ------------------------------------------------------------

    @contextmanager
    def _tx(self, user_id: Optional[str]) -> Iterator[Any]:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row

        try:
            with self._pool.connection() as conn:
                conn.row_factory = dict_row
                with conn.transaction():
                    if user_id is not None:
                        subject = str(uuid.UUID(user_id))
                        conn.execute("select set_config('request.jwt.claims', %s, true)",
                                     [json.dumps({"sub": subject, "role": "authenticated"})])
                        conn.execute(sql.SQL("set local role {}").format(sql.Identifier(self._user_role)))
                    yield conn
        except psycopg.errors.UniqueViolation as error:
            raise ConflictError("a record with the same unique values already exists") from error
        except psycopg.errors.InsufficientPrivilege as error:
            raise ForbiddenError("not allowed in this workspace") from error
        except psycopg.errors.CheckViolation as error:
            raise ValidationError(f"rejected by the database: {error.diag.message_primary}") from error
        except psycopg.errors.NotNullViolation as error:
            raise ValidationError(f"missing a required value: {error.diag.column_name}") from error

    def _scope(self, ctx: Ctx) -> Optional[str]:
        return None if ctx.system else ctx.user_id

    @staticmethod
    def _adapt(spec: EntitySpec, name: str, value: Any) -> Any:
        from psycopg.types.json import Jsonb

        col = spec.columns.get(name) or COMMON_COLUMNS.get(name)
        if col is not None and col.kind == "json" and value is not None:
            return Jsonb(value)
        return value

    def _where(self, spec: EntitySpec, ctx: Ctx, filters, q) -> Tuple[Any, List[Any]]:
        from psycopg import sql

        clauses = [sql.SQL("workspace_id = %s")]
        params: List[Any] = [ctx.workspace_id]
        for name, op, value in filters:
            ident = sql.Identifier(name)
            col = spec.columns.get(name) or COMMON_COLUMNS.get(name)
            if op == "isnull":
                clauses.append(sql.SQL("{} is null" if value else "{} is not null").format(ident))
            elif op == "in":
                non_null = [v for v in value if v is not None]
                parts = []
                if non_null:
                    parts.append(sql.SQL("{} = any(%s)").format(ident))
                    params.append(non_null)
                if len(non_null) != len(value):
                    parts.append(sql.SQL("{} is null").format(ident))
                clauses.append(sql.SQL("(") + sql.SQL(" or ").join(parts or [sql.SQL("false")]) + sql.SQL(")"))
            elif op == "contains":
                if col.kind == "tags":
                    clauses.append(sql.SQL("%s = any({})").format(ident))
                else:
                    clauses.append(sql.SQL("{}::text ilike %s").format(ident))
                    value = f"%{value}%"
                params.append(value)
            elif op == "ilike":
                escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append(sql.SQL("{}::text ilike %s").format(ident))
                params.append(f"%{escaped}%")
            else:
                symbol = {"eq": "=", "ne": "is distinct from", "gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}[op]
                clauses.append(sql.SQL("{} " + symbol + " %s").format(ident))
                params.append(self._adapt(spec, name, value))
        if q and spec.searchable:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            ors = [sql.SQL("{}::text ilike %s").format(sql.Identifier(c)) for c in spec.searchable]
            clauses.append(sql.SQL("(") + sql.SQL(" or ").join(ors) + sql.SQL(")"))
            params.extend([f"%{escaped}%"] * len(ors))
        return sql.SQL(" and ").join(clauses), params

    def _table(self, spec: EntitySpec):
        from psycopg import sql

        return sql.Identifier("careercloud", spec.table)

    # --- Store primitives -----------------------------------------------------

    def _insert(self, ctx: Ctx, spec: EntitySpec, row: Dict[str, Any]) -> Dict[str, Any]:
        from psycopg import sql

        names = list(row)
        query = sql.SQL("insert into {} ({}) values ({}) returning *").format(
            self._table(spec),
            sql.SQL(", ").join(sql.Identifier(n) for n in names),
            sql.SQL(", ").join(sql.Placeholder() for _ in names),
        )
        with self._tx(self._scope(ctx)) as conn:
            if not ctx.system and not self._has_role(conn, ctx, write=True):
                raise ForbiddenError("not a writer in this workspace")
            result = conn.execute(query, [self._adapt(spec, n, row[n]) for n in names]).fetchone()
        return _plain(result)

    #: Rows per pipelined ``executemany`` call inside one transaction.
    BULK_CHUNK = 1000

    def _insert_many(self, ctx: Ctx, spec: EntitySpec, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """One transaction, one permission check, and the same INSERT as :meth:`_insert`
        sent through psycopg's pipeline (a few round trips per chunk instead of several
        per row). RLS still applies to every row."""
        from psycopg import sql

        by_id: Dict[str, Dict[str, Any]] = {}
        groups: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(tuple(row), []).append(row)
        with self._tx(self._scope(ctx)) as conn:
            if not ctx.system and not self._has_role(conn, ctx, write=True):
                raise ForbiddenError("not a writer in this workspace")
            for names, group in groups.items():
                query = sql.SQL("insert into {} ({}) values ({}) returning *").format(
                    self._table(spec), sql.SQL(", ").join(sql.Identifier(n) for n in names),
                    sql.SQL(", ").join(sql.Placeholder() for _ in names))
                with conn.cursor() as cur:
                    for start in range(0, len(group), self.BULK_CHUNK):
                        chunk = group[start:start + self.BULK_CHUNK]
                        cur.executemany(query, [[self._adapt(spec, n, r[n]) for n in names] for r in chunk],
                                        returning=True)
                        while True:
                            result = cur.fetchone()
                            if result is not None:
                                by_id[result["id"]] = _plain(result)
                            if not cur.nextset():
                                break
        return [by_id[row["id"]] for row in rows if row["id"] in by_id]

    def _update_many(self, ctx: Ctx, spec: EntitySpec, changes: List[Tuple[str, Dict[str, Any]]]
                     ) -> List[Dict[str, Any]]:
        """The same UPDATE as :meth:`_update` (workspace filter + RLS), pipelined in one
        transaction, grouped by the set of changed columns."""
        from psycopg import sql

        out: List[Dict[str, Any]] = []
        groups: Dict[Tuple[str, ...], List[Tuple[str, Dict[str, Any]]]] = {}
        for row_id, values in changes:
            groups.setdefault(tuple(values), []).append((row_id, values))
        with self._tx(self._scope(ctx)) as conn:
            if not ctx.system and not self._has_role(conn, ctx, write=True):
                raise ForbiddenError("this workspace role is read-only")
            for names, group in groups.items():
                query = sql.SQL("update {} set {} where id = %s and workspace_id = %s returning *").format(
                    self._table(spec), sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(n))
                                                          for n in names))
                with conn.cursor() as cur:
                    for start in range(0, len(group), self.BULK_CHUNK):
                        chunk = group[start:start + self.BULK_CHUNK]
                        cur.executemany(query, [[self._adapt(spec, n, values[n]) for n in names]
                                                + [row_id, ctx.workspace_id] for row_id, values in chunk],
                                        returning=True)
                        while True:
                            result = cur.fetchone()
                            if result is not None:
                                out.append(_plain(result))
                            if not cur.nextset():
                                break
        return out

    def _delete_many(self, ctx: Ctx, spec: EntitySpec, row_ids: List[str]) -> int:
        from psycopg import sql

        query = sql.SQL("delete from {} where id = any(%s) and workspace_id = %s").format(self._table(spec))
        deleted = 0
        with self._tx(self._scope(ctx)) as conn:
            if not ctx.system and not self._has_role(conn, ctx, write=True):
                raise ForbiddenError("this workspace role is read-only")
            for start in range(0, len(row_ids), 5000):
                deleted += conn.execute(query, [row_ids[start:start + 5000], ctx.workspace_id]).rowcount
        return deleted

    def _has_role(self, conn: Any, ctx: Ctx, *, write: bool) -> bool:
        role = conn.execute("select careercloud.member_role(%s::uuid) as role", [ctx.workspace_id]).fetchone()["role"]
        if role is None:
            raise NotFoundError("workspace not found")
        return role in ("owner", "admin", "manager", "member") if write else True

    def _update(self, ctx: Ctx, spec: EntitySpec, row_id: str, changes: Dict[str, Any],
                expected_version: Optional[int]) -> Optional[Dict[str, Any]]:
        from psycopg import sql

        sets = [sql.SQL("{} = %s").format(sql.Identifier(n)) for n in changes]
        params = [self._adapt(spec, n, v) for n, v in changes.items()] + [row_id, ctx.workspace_id]
        where = sql.SQL("id = %s and workspace_id = %s")
        if expected_version is not None:
            where = where + sql.SQL(" and version = %s")
            params.append(expected_version)
        query = sql.SQL("update {} set {} where {} returning *").format(
            self._table(spec), sql.SQL(", ").join(sets), where)
        with self._tx(self._scope(ctx)) as conn:
            if not ctx.system and not self._has_role(conn, ctx, write=True):
                raise ForbiddenError("this workspace role is read-only")
            row = conn.execute(query, params).fetchone()
        return _plain(row) if row else None

    def _delete(self, ctx: Ctx, spec: EntitySpec, row_id: str) -> bool:
        from psycopg import sql

        query = sql.SQL("delete from {} where id = %s and workspace_id = %s").format(self._table(spec))
        with self._tx(self._scope(ctx)) as conn:
            if not ctx.system and not self._has_role(conn, ctx, write=True):
                raise ForbiddenError("this workspace role is read-only")
            return conn.execute(query, [row_id, ctx.workspace_id]).rowcount > 0

    def _get(self, ctx: Ctx, spec: EntitySpec, row_id: str) -> Optional[Dict[str, Any]]:
        from psycopg import sql

        query = sql.SQL("select * from {} where id = %s and workspace_id = %s").format(self._table(spec))
        with self._tx(self._scope(ctx)) as conn:
            row = conn.execute(query, [row_id, ctx.workspace_id]).fetchone()
        return _plain(row) if row else None

    def _list(self, ctx: Ctx, spec: EntitySpec, filters, q, order, limit, offset) -> Page:
        from psycopg import sql

        where, params = self._where(spec, ctx, filters, q)
        order_sql = sql.SQL(", ").join(
            sql.SQL("{} {} nulls last").format(sql.Identifier(n), sql.SQL("desc" if d else "asc")) for n, d in order)
        with self._tx(self._scope(ctx)) as conn:
            total = conn.execute(sql.SQL("select count(*) as n from {} where {}").format(self._table(spec), where),
                                 params).fetchone()["n"]
            rows = conn.execute(
                sql.SQL("select * from {} where {} order by {} limit %s offset %s").format(
                    self._table(spec), where, order_sql),
                params + [limit, offset]).fetchall()
        return Page([_plain(r) for r in rows], total, limit, offset)

    def _group_count(self, ctx: Ctx, spec: EntitySpec, column: str, filters, q) -> Dict[Any, int]:
        from psycopg import sql

        where, params = self._where(spec, ctx, filters, q)
        col = spec.columns.get(column) or COMMON_COLUMNS[column]
        expr = sql.SQL("unnest({})").format(sql.Identifier(column)) if col.kind == "tags" else sql.Identifier(column)
        query = sql.SQL("select {} as value, count(*) as n from {} where {} group by 1").format(
            expr, self._table(spec), where)
        with self._tx(self._scope(ctx)) as conn:
            rows = conn.execute(query, params).fetchall()
        return {(str(r["value"]) if isinstance(r["value"], uuid.UUID) else r["value"]): r["n"] for r in rows}

    # --- workspaces --------------------------------------------------------

    def create_workspace(self, user_id: str, name: str, slug: str) -> Dict[str, Any]:
        with self._tx(user_id) as conn:
            ws_id = conn.execute("select careercloud.create_workspace(%s, %s) as id", [name, slug]).fetchone()["id"]
            row = conn.execute("select * from careercloud.workspaces where id = %s", [ws_id]).fetchone()
        return {**_plain(row), "role": "owner"}

    def workspaces_for(self, user_id: str) -> List[Dict[str, Any]]:
        with self._tx(user_id) as conn:
            rows = conn.execute(
                "select w.*, m.role from careercloud.workspaces w join careercloud.workspace_members m "
                "on m.workspace_id = w.id where m.user_id = auth.uid() order by w.created_at").fetchall()
        return [_plain(r) for r in rows]

    def membership(self, user_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
        try:
            workspace_id = str(uuid.UUID(workspace_id))
        except (ValueError, TypeError):
            return None
        with self._tx(user_id) as conn:
            row = conn.execute(
                "select w.id as workspace_id, w.name, w.slug, w.ai_external_allowed, w.settings, m.role "
                "from careercloud.workspaces w join careercloud.workspace_members m on m.workspace_id = w.id "
                "where w.id = %s and m.user_id = auth.uid()", [workspace_id]).fetchone()
        return _plain(row) if row else None

    def system_membership(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        with self._tx(None) as conn:
            row = conn.execute("select id as workspace_id, name, slug, ai_external_allowed, settings "
                               "from careercloud.workspaces where id = %s", [workspace_id]).fetchone()
        return _plain(row) if row else None

    def add_member(self, ctx: Ctx, user_id: str, role: str) -> None:
        if role not in ("admin", "manager", "member", "viewer"):
            raise ValidationError("role must be admin, manager, member or viewer")
        with self._tx(self._scope(ctx)) as conn:
            conn.execute(
                "insert into careercloud.workspace_members (workspace_id, user_id, role) values (%s, %s, %s) "
                "on conflict (workspace_id, user_id) do update set role = excluded.role",
                [ctx.workspace_id, str(uuid.UUID(user_id)), role])

    def remove_member(self, ctx: Ctx, user_id: str) -> bool:
        with self._tx(self._scope(ctx)) as conn:
            row = conn.execute("select role from careercloud.workspace_members where workspace_id = %s and user_id = %s",
                               [ctx.workspace_id, str(uuid.UUID(user_id))]).fetchone()
            if row is None:
                return False
            if row["role"] == "owner":
                raise ValidationError("the workspace owner cannot be removed")
            conn.execute("delete from careercloud.workspace_members where workspace_id = %s and user_id = %s "
                         "and role <> 'owner'", [ctx.workspace_id, str(uuid.UUID(user_id))])
        return True

    def list_members(self, ctx: Ctx) -> List[Dict[str, Any]]:
        with self._tx(self._scope(ctx)) as conn:
            rows = conn.execute("select user_id, role from careercloud.workspace_members where workspace_id = %s",
                                [ctx.workspace_id]).fetchall()
        return [_plain(r) for r in rows]

    def update_workspace(self, ctx: Ctx, **changes: Any) -> Dict[str, Any]:
        from psycopg import sql
        from psycopg.types.json import Jsonb

        allowed = {"name", "settings", "ai_external_allowed"}
        bad = set(changes) - allowed
        if bad:
            raise ValidationError(f"cannot change {', '.join(sorted(bad))}")
        if not changes:
            raise ValidationError("nothing to change")
        sets = [sql.SQL("{} = %s").format(sql.Identifier(k)) for k in changes]
        values = [Jsonb(v) if k == "settings" else v for k, v in changes.items()]
        with self._tx(self._scope(ctx)) as conn:
            row = conn.execute(
                sql.SQL("update careercloud.workspaces set {}, updated_at = now() where id = %s returning *").format(
                    sql.SQL(", ").join(sets)), values + [ctx.workspace_id]).fetchone()
        if row is None:
            raise ForbiddenError("workspace admin rights required")
        return _plain(row)


def _pg_list_workspace_ids(self: PostgresStore) -> List[str]:
    with self._tx(None) as conn:
        return [str(r["id"]) for r in conn.execute("select id from careercloud.workspaces").fetchall()]


PostgresStore.list_workspace_ids = _pg_list_workspace_ids  # type: ignore[attr-defined]
